from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from torch.utils.data import DataLoader

from student.checkpoint import load_student_bundle
from student.data import GraffitiTrainingDataset, LabelMaps, collate_training_batch, create_eval_transform, load_jsonl
from student.trainer import evaluate_model


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate a trained graffiti student bundle.")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--num-workers", type=int, default=2)
    parser.add_argument("--device", default="cpu")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    import torch

    device = torch.device(args.device)
    model, processor, _ = load_student_bundle(args.model_dir.resolve(), device)
    image_mean = list(getattr(processor, "image_mean", [0.5, 0.5, 0.5]))
    image_std = list(getattr(processor, "image_std", [0.5, 0.5, 0.5]))
    rows = load_jsonl(args.manifest.resolve())
    dataset = GraffitiTrainingDataset(
        rows,
        transform=create_eval_transform(224, image_mean, image_std),
        label_maps=LabelMaps.default(),
    )
    loader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.num_workers,
        pin_memory=True,
        collate_fn=collate_training_batch,
    )
    metrics, thresholds = evaluate_model(model, loader, rows)
    payload = {"metrics": metrics, "thresholds": thresholds}
    text = json.dumps(payload, indent=2, ensure_ascii=True)
    if args.output:
        args.output.resolve().write_text(text + "\n", encoding="utf-8")
        print(f"Wrote metrics to {args.output.resolve()}")
    else:
        print(text)


if __name__ == "__main__":
    main()
