from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import torch
from safetensors.torch import load_file
from torch import nn
from torch.utils.data import DataLoader

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from student.checkpoint import load_student_bundle
from student.constants import CORE_MEDIA, SCORE_FIELDS
from student.data import GraffitiTrainingDataset, LabelMaps, collate_training_batch, create_eval_transform, load_jsonl
from student.metrics import build_prediction_record, compute_multitask_metrics, tune_binary_threshold
from student.model import GraffitiStudentModel, StudentModelConfig
from student.trainer import evaluate_model


OUTPUT_NAMES = [
    "usable_logits",
    "medium_logits",
    "color_applicable_logits",
    "overall_score",
    *SCORE_FIELDS,
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark PyTorch FP32 CPU vs ONNX FP32 CPU for a student bundle.")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--onnx-path", type=Path, default=None)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--num-workers", type=int, default=0)
    parser.add_argument("--latency-samples", type=int, default=16)
    parser.add_argument("--warmup-runs", type=int, default=2)
    parser.add_argument("--opset", type=int, default=17)
    return parser.parse_args()


class OnnxExportWrapper(nn.Module):
    def __init__(self, model: nn.Module) -> None:
        super().__init__()
        self.model = model

    def forward(self, pixel_values: torch.Tensor):
        outputs = self.model(pixel_values=pixel_values)
        return tuple(outputs[name] for name in OUTPUT_NAMES)


def sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def load_rows_and_dataset(model_dir: Path, manifest: Path):
    _, processor, _ = load_student_bundle(model_dir.resolve(), torch.device("cpu"))
    image_mean = list(getattr(processor, "image_mean", [0.5, 0.5, 0.5]))
    image_std = list(getattr(processor, "image_std", [0.5, 0.5, 0.5]))
    rows = load_jsonl(manifest.resolve())
    dataset = GraffitiTrainingDataset(
        rows,
        transform=create_eval_transform(224, image_mean, image_std),
        label_maps=LabelMaps.default(),
    )
    loader = DataLoader(
        dataset,
        batch_size=8,
        shuffle=False,
        num_workers=0,
        pin_memory=False,
        collate_fn=collate_training_batch,
    )
    return rows, dataset, processor, loader


def materialize_model_for_export(model_dir: Path) -> nn.Module:
    student_config = StudentModelConfig.from_dict(
        json.loads((model_dir / "student_config.json").read_text(encoding="utf-8"))
    )
    student_config.attn_implementation = "eager"
    model = GraffitiStudentModel(student_config, load_pretrained_backbone=False)
    state_dict = load_file(str(model_dir / "model.safetensors"))
    model.load_state_dict(state_dict)
    model = model.cpu().eval()
    backbone = getattr(model, "backbone", None)
    if backbone is not None and hasattr(backbone, "merge_and_unload"):
        model.backbone = backbone.merge_and_unload()
    return model


def export_to_onnx(model: nn.Module, onnx_path: Path, *, opset: int) -> None:
    onnx_path.parent.mkdir(parents=True, exist_ok=True)
    wrapper = OnnxExportWrapper(model).eval()
    dummy = torch.randn(1, 3, 224, 224, dtype=torch.float32)
    with torch.backends.cuda.sdp_kernel(enable_flash=False, enable_mem_efficient=False, enable_math=True):
        torch.onnx.export(
            wrapper,
            (dummy,),
            str(onnx_path),
            export_params=True,
            opset_version=opset,
            do_constant_folding=True,
            input_names=["pixel_values"],
            output_names=OUTPUT_NAMES,
            dynamic_axes={
                "pixel_values": {0: "batch"},
                **{name: {0: "batch"} for name in OUTPUT_NAMES},
            },
        )


def build_onnx_session(onnx_path: Path):
    import onnxruntime as ort

    return ort.InferenceSession(
        str(onnx_path),
        providers=["CPUExecutionProvider"],
    )


def evaluate_onnx_session(session, loader: DataLoader, rows: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, float]]:
    row_by_file = {row["file"]: row for row in rows}
    medium_labels = list(LabelMaps.default().medium_to_index.keys())
    probability_records: list[dict[str, Any]] = []

    for batch in loader:
        ort_outputs = session.run(
            OUTPUT_NAMES,
            {"pixel_values": batch["pixel_values"].cpu().numpy().astype(np.float32)},
        )
        outputs = {name: value for name, value in zip(OUTPUT_NAMES, ort_outputs)}

        usable_probabilities = [sigmoid(float(value)) for value in outputs["usable_logits"]]
        color_probabilities = [sigmoid(float(value)) for value in outputs["color_applicable_logits"]]
        medium_predictions = outputs["medium_logits"].argmax(axis=-1).tolist()
        overall_predictions = outputs["overall_score"].tolist()
        score_predictions = {field: outputs[field].tolist() for field in SCORE_FIELDS}

        for index, file_name in enumerate(batch["file"]):
            row = row_by_file[file_name]
            raw_scores = {
                "color_applicable_probability": color_probabilities[index],
                "overall_score": float(overall_predictions[index]),
            }
            for field in SCORE_FIELDS:
                raw_scores[field] = float(score_predictions[field][index])
            probability_records.append(
                {
                    "row": row,
                    "usable_probability": usable_probabilities[index],
                    "medium_prediction": medium_labels[int(medium_predictions[index])],
                    "raw_scores": raw_scores,
                }
            )

    usable_targets = [1 if record["row"].get("image_usable") else 0 for record in probability_records]
    usable_probabilities = [record["usable_probability"] for record in probability_records]
    usable_threshold = tune_binary_threshold(usable_targets, usable_probabilities, min_recall=0.95)

    color_candidates = [
        record for record in probability_records
        if record["row"].get("image_usable") and record["row"].get("medium") in CORE_MEDIA
    ]
    if color_candidates:
        color_targets = [1 if record["row"].get("color_harmony") is not None else 0 for record in color_candidates]
        color_probabilities = [record["raw_scores"]["color_applicable_probability"] for record in color_candidates]
        color_threshold = tune_binary_threshold(color_targets, color_probabilities)
    else:
        color_threshold = 0.5

    records = [
        build_prediction_record(
            usable_probability=record["usable_probability"],
            usable_threshold=usable_threshold,
            medium_target=record["row"].get("medium"),
            medium_prediction=record["medium_prediction"],
            score_domain_target=bool(record["row"].get("image_usable")) and record["row"].get("medium") in CORE_MEDIA,
            raw_scores=record["raw_scores"],
            row=record["row"],
            color_threshold=color_threshold,
        )
        for record in probability_records
    ]
    metrics = compute_multitask_metrics(records, usable_threshold=usable_threshold, color_threshold=color_threshold)
    thresholds = {"usable": usable_threshold, "color_applicable": color_threshold}
    return metrics, thresholds


def benchmark_single_image_latency_pytorch(
    model: nn.Module,
    dataset: GraffitiTrainingDataset,
    *,
    samples: int,
    warmup_runs: int,
) -> dict[str, float]:
    selected = [dataset[index % len(dataset)] for index in range(max(samples, 1))]

    for _ in range(max(warmup_runs, 0)):
        sample = selected[0]
        with torch.inference_mode():
            _ = model(pixel_values=sample["pixel_values"].unsqueeze(0))

    first_sample = selected[0]
    started = time.perf_counter()
    with torch.inference_mode():
        _ = model(pixel_values=first_sample["pixel_values"].unsqueeze(0))
    first_hit_ms = (time.perf_counter() - started) * 1000.0

    latencies: list[float] = []
    for sample in selected:
        started = time.perf_counter()
        with torch.inference_mode():
            _ = model(pixel_values=sample["pixel_values"].unsqueeze(0))
        latencies.append((time.perf_counter() - started) * 1000.0)

    sorted_latencies = sorted(latencies)
    p95_index = max(0, min(len(sorted_latencies) - 1, int(round(0.95 * (len(sorted_latencies) - 1)))))
    return {
        "first_hit_ms": round(first_hit_ms, 2),
        "avg_warm_ms": round(statistics.mean(latencies), 2),
        "p95_warm_ms": round(sorted_latencies[p95_index], 2),
    }


def benchmark_single_image_latency_onnx(
    session,
    dataset: GraffitiTrainingDataset,
    *,
    samples: int,
    warmup_runs: int,
) -> dict[str, float]:
    selected = [dataset[index % len(dataset)] for index in range(max(samples, 1))]

    for _ in range(max(warmup_runs, 0)):
        sample = selected[0]
        _ = session.run(None, {"pixel_values": sample["pixel_values"].unsqueeze(0).numpy().astype(np.float32)})

    first_sample = selected[0]
    started = time.perf_counter()
    _ = session.run(None, {"pixel_values": first_sample["pixel_values"].unsqueeze(0).numpy().astype(np.float32)})
    first_hit_ms = (time.perf_counter() - started) * 1000.0

    latencies: list[float] = []
    for sample in selected:
        started = time.perf_counter()
        _ = session.run(None, {"pixel_values": sample["pixel_values"].unsqueeze(0).numpy().astype(np.float32)})
        latencies.append((time.perf_counter() - started) * 1000.0)

    sorted_latencies = sorted(latencies)
    p95_index = max(0, min(len(sorted_latencies) - 1, int(round(0.95 * (len(sorted_latencies) - 1)))))
    return {
        "first_hit_ms": round(first_hit_ms, 2),
        "avg_warm_ms": round(statistics.mean(latencies), 2),
        "p95_warm_ms": round(sorted_latencies[p95_index], 2),
    }


def main() -> None:
    args = parse_args()
    model_dir = args.model_dir.resolve()
    onnx_path = args.onnx_path.resolve() if args.onnx_path else (model_dir.parent / "model_fp32.onnx").resolve()
    rows, dataset, _, loader = load_rows_and_dataset(model_dir, args.manifest)

    pytorch_model = materialize_model_for_export(model_dir)
    export_to_onnx(pytorch_model, onnx_path, opset=args.opset)
    onnx_session = build_onnx_session(onnx_path)

    pytorch_metrics, pytorch_thresholds = evaluate_model(pytorch_model, loader, rows)
    onnx_metrics, onnx_thresholds = evaluate_onnx_session(onnx_session, loader, rows)

    payload = {
        "model_dir": str(model_dir),
        "manifest": str(args.manifest.resolve()),
        "onnx_path": str(onnx_path),
        "bundle_sizes": {
            "pytorch_safetensors_bytes": (model_dir / "model.safetensors").stat().st_size,
            "onnx_bytes": onnx_path.stat().st_size,
        },
        "pytorch_fp32_cpu": {
            "metrics": pytorch_metrics,
            "thresholds": pytorch_thresholds,
            "latency": benchmark_single_image_latency_pytorch(
                pytorch_model,
                dataset,
                samples=args.latency_samples,
                warmup_runs=args.warmup_runs,
            ),
        },
        "onnx_fp32_cpu": {
            "metrics": onnx_metrics,
            "thresholds": onnx_thresholds,
            "latency": benchmark_single_image_latency_onnx(
                onnx_session,
                dataset,
                samples=args.latency_samples,
                warmup_runs=args.warmup_runs,
            ),
        },
    }

    text = json.dumps(payload, indent=2, ensure_ascii=True)
    if args.output:
        args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
        args.output.resolve().write_text(text + "\n", encoding="utf-8")
        print(f"Wrote ONNX benchmark to {args.output.resolve()}")
    else:
        print(text)


if __name__ == "__main__":
    main()
