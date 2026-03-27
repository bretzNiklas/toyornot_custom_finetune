from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from student.trainer import TrainingConfig, train


PRESETS = {
    "vit_base_224": {
        "model_name": "google/vit-base-patch16-224-in21k",
        "use_lora": True,
        "image_size": 224,
        "batch_size": 8,
        "grad_accum_steps": 2,
    },
    "vit_base_384": {
        "model_name": "google/vit-base-patch16-224-in21k",
        "use_lora": True,
        "image_size": 384,
        "batch_size": 4,
        "grad_accum_steps": 4,
    },
    "dinov2_base_224": {
        "model_name": "facebook/dinov2-base",
        "use_lora": True,
        "image_size": 224,
        "batch_size": 6,
        "grad_accum_steps": 2,
    },
    "convnextv2_tiny_224": {
        "model_name": "facebook/convnextv2-tiny-1k-224",
        "use_lora": False,
        "image_size": 224,
        "batch_size": 8,
        "grad_accum_steps": 2,
    },
    "efficientnet_b0_224": {
        "model_name": "google/efficientnet-b0",
        "use_lora": False,
        "image_size": 224,
        "batch_size": 12,
        "grad_accum_steps": 2,
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a full Stage A -> Stage B -> test eval experiment.")
    parser.add_argument("--preset", choices=sorted(PRESETS), required=True)
    parser.add_argument("--name", required=True, help="Experiment name, e.g. dinov2_base_224_run1")
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=Path("exports/student/v1"),
        help="Directory containing student manifests.",
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=Path("runs/benchmarks"),
        help="Where to write experiment outputs.",
    )
    parser.add_argument("--mixed-precision", default="fp16")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--stage-a-epochs", type=int, default=10)
    parser.add_argument("--stage-b-epochs", type=int, default=5)
    return parser.parse_args()


def load_test_metrics(bundle_dir: Path, test_manifest: Path) -> dict:
    import torch
    from torch.utils.data import DataLoader

    from student.checkpoint import load_student_bundle
    from student.data import GraffitiTrainingDataset, LabelMaps, collate_training_batch, create_eval_transform, load_jsonl
    from student.trainer import evaluate_model

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model, processor, thresholds = load_student_bundle(bundle_dir, device)
    rows = load_jsonl(test_manifest)
    image_mean = list(getattr(processor, "image_mean", [0.5, 0.5, 0.5]))
    image_std = list(getattr(processor, "image_std", [0.5, 0.5, 0.5]))
    image_size = json.loads((bundle_dir / "training_config.json").read_text(encoding="utf-8")).get("image_size", 224)
    dataset = GraffitiTrainingDataset(
        rows,
        transform=create_eval_transform(image_size, image_mean, image_std),
        label_maps=LabelMaps.default(),
    )
    loader = DataLoader(
        dataset,
        batch_size=8,
        shuffle=False,
        num_workers=2,
        pin_memory=True,
        collate_fn=collate_training_batch,
    )
    metrics, fresh_thresholds = evaluate_model(model, loader, rows)
    return {"metrics": metrics, "thresholds": thresholds or fresh_thresholds}


def main() -> None:
    args = parse_args()
    preset = PRESETS[args.preset]
    artifacts = args.artifacts_dir.resolve()
    run_root = (args.runs_dir / args.name).resolve()
    run_root.mkdir(parents=True, exist_ok=True)

    stage_a_config = TrainingConfig(
        stage="stage_a",
        train_manifest=str((artifacts / "stage_a_train_v1.jsonl").resolve()),
        val_manifest=str((artifacts / "human_val_locked_v1.jsonl").resolve()),
        output_dir=str((run_root / "stage_a").resolve()),
        model_name=preset["model_name"],
        use_lora=preset["use_lora"],
        epochs=args.stage_a_epochs,
        batch_size=preset["batch_size"],
        grad_accum_steps=preset["grad_accum_steps"],
        image_size=preset["image_size"],
        mixed_precision=args.mixed_precision,
        seed=args.seed,
    )
    print(f"Running Stage A for {args.name} with preset {args.preset}")
    stage_a_bundle = train(stage_a_config)

    stage_b_config = replace(
        stage_a_config,
        stage="stage_b",
        train_manifest=str((artifacts / "human_train_v1.jsonl").resolve()),
        output_dir=str((run_root / "stage_b").resolve()),
        resume_from=str(stage_a_bundle.resolve()),
        epochs=args.stage_b_epochs,
        learning_rate=3e-5,
    )
    print(f"Running Stage B for {args.name}")
    stage_b_bundle = train(stage_b_config)

    print(f"Evaluating {args.name} on locked human test")
    test_manifest = (artifacts / "human_test_locked_v1.jsonl").resolve()
    test_payload = load_test_metrics(stage_b_bundle, test_manifest)
    report = {
        "experiment_name": args.name,
        "preset": args.preset,
        "stage_a_bundle": str(stage_a_bundle),
        "stage_b_bundle": str(stage_b_bundle),
        "test_metrics": test_payload["metrics"],
        "thresholds": test_payload["thresholds"],
    }
    report_path = run_root / "report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    print(f"Wrote report to {report_path}")
    print(json.dumps(report["test_metrics"], indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
