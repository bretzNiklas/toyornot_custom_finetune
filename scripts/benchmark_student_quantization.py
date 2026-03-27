from __future__ import annotations

import argparse
import copy
import io
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import torch
from PIL import Image
from torch import nn
from torch.utils.data import DataLoader

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from student.checkpoint import load_student_bundle
from student.data import GraffitiTrainingDataset, LabelMaps, collate_training_batch, create_eval_transform, load_jsonl
from student.trainer import evaluate_model


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark FP32 vs dynamic INT8 CPU inference for a student bundle.")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--num-workers", type=int, default=0)
    parser.add_argument("--latency-samples", type=int, default=16)
    parser.add_argument("--warmup-runs", type=int, default=2)
    return parser.parse_args()


def build_eval_loader(model_dir: Path, manifest: Path, batch_size: int, num_workers: int):
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
        batch_size=batch_size,
        shuffle=False,
        num_workers=num_workers,
        pin_memory=False,
        collate_fn=collate_training_batch,
    )
    return rows, dataset, processor, loader


def quantize_dynamic_int8(model: nn.Module) -> nn.Module:
    materialized = copy.deepcopy(model).cpu().eval()
    backbone = getattr(materialized, "backbone", None)
    if backbone is not None and hasattr(backbone, "merge_and_unload"):
        materialized.backbone = backbone.merge_and_unload()
    quantized = torch.ao.quantization.quantize_dynamic(
        materialized,
        {nn.Linear},
        dtype=torch.qint8,
    )
    return quantized


def serialized_state_dict_bytes(model: nn.Module) -> int:
    buffer = io.BytesIO()
    torch.save(model.state_dict(), buffer)
    return len(buffer.getvalue())


def benchmark_single_image_latency(
    model: nn.Module,
    dataset: GraffitiTrainingDataset,
    *,
    samples: int,
    warmup_runs: int,
) -> dict[str, float]:
    if len(dataset) == 0:
        return {
            "first_hit_ms": 0.0,
            "avg_warm_ms": 0.0,
            "p95_warm_ms": 0.0,
        }

    model.eval()
    device = torch.device("cpu")

    selected = [dataset[index % len(dataset)] for index in range(max(samples, 1))]

    for _ in range(max(warmup_runs, 0)):
        sample = selected[0]
        with torch.inference_mode():
            _ = model(pixel_values=sample["pixel_values"].unsqueeze(0).to(device))

    first_sample = selected[0]
    started = time.perf_counter()
    with torch.inference_mode():
        _ = model(pixel_values=first_sample["pixel_values"].unsqueeze(0).to(device))
    first_hit_ms = (time.perf_counter() - started) * 1000.0

    warm_latencies: list[float] = []
    for sample in selected:
        started = time.perf_counter()
        with torch.inference_mode():
            _ = model(pixel_values=sample["pixel_values"].unsqueeze(0).to(device))
        warm_latencies.append((time.perf_counter() - started) * 1000.0)

    sorted_latencies = sorted(warm_latencies)
    p95_index = max(0, min(len(sorted_latencies) - 1, int(round(0.95 * (len(sorted_latencies) - 1)))))
    return {
        "first_hit_ms": round(first_hit_ms, 2),
        "avg_warm_ms": round(statistics.mean(warm_latencies), 2),
        "p95_warm_ms": round(sorted_latencies[p95_index], 2),
    }


def evaluate_variant(
    *,
    model: nn.Module,
    loader: DataLoader,
    rows: list[dict[str, Any]],
    dataset: GraffitiTrainingDataset,
    latency_samples: int,
    warmup_runs: int,
) -> dict[str, Any]:
    metrics, thresholds = evaluate_model(model, loader, rows)
    latency = benchmark_single_image_latency(
        model,
        dataset,
        samples=latency_samples,
        warmup_runs=warmup_runs,
    )
    return {
        "metrics": metrics,
        "thresholds": thresholds,
        "latency": latency,
    }


def main() -> None:
    args = parse_args()
    model_dir = args.model_dir.resolve()
    rows, dataset, _, loader = build_eval_loader(
        model_dir,
        args.manifest,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
    )

    fp32_model, _, _ = load_student_bundle(model_dir, torch.device("cpu"))
    int8_model = quantize_dynamic_int8(fp32_model)

    payload = {
        "model_dir": str(model_dir),
        "manifest": str(args.manifest.resolve()),
        "bundle_sizes": {
            "fp32_safetensors_bytes": (model_dir / "model.safetensors").stat().st_size,
            "dynamic_int8_state_dict_bytes": serialized_state_dict_bytes(int8_model),
        },
        "fp32_cpu": evaluate_variant(
            model=fp32_model,
            loader=loader,
            rows=rows,
            dataset=dataset,
            latency_samples=args.latency_samples,
            warmup_runs=args.warmup_runs,
        ),
        "dynamic_int8_cpu": evaluate_variant(
            model=int8_model,
            loader=loader,
            rows=rows,
            dataset=dataset,
            latency_samples=args.latency_samples,
            warmup_runs=args.warmup_runs,
        ),
    }

    text = json.dumps(payload, indent=2, ensure_ascii=True)
    if args.output:
        args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
        args.output.resolve().write_text(text + "\n", encoding="utf-8")
        print(f"Wrote quantization benchmark to {args.output.resolve()}")
    else:
        print(text)


if __name__ == "__main__":
    main()
