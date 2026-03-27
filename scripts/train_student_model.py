from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from student.trainer import TrainingConfig, train


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train the graffiti student model.")
    parser.add_argument("--stage", choices=["stage_a", "stage_b"], required=True)
    parser.add_argument("--train-manifest", type=Path, required=True)
    parser.add_argument("--val-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model-name", default="google/vit-base-patch16-224-in21k")
    parser.add_argument("--no-lora", action="store_true", help="Disable LoRA and fine-tune the full backbone.")
    parser.add_argument("--resume-from", type=Path, default=None)
    parser.add_argument("--epochs", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--grad-accum-steps", type=int, default=2)
    parser.add_argument("--learning-rate", type=float, default=None)
    parser.add_argument("--weight-decay", type=float, default=0.01)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--num-workers", type=int, default=2)
    parser.add_argument("--image-size", type=int, default=224)
    parser.add_argument("--mixed-precision", default="fp16")
    parser.add_argument("--teacher-weight", type=float, default=0.35)
    parser.add_argument("--unusable-boost", type=float, default=8.0)
    parser.add_argument("--wall-boost", type=float, default=1.5)
    parser.add_argument("--side-domain-weight", type=float, default=0.6)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.stage == "stage_a":
        default_epochs = 10
        default_lr = 1e-4
    else:
        default_epochs = 5
        default_lr = 3e-5

    config = TrainingConfig(
        stage=args.stage,
        train_manifest=str(args.train_manifest.resolve()),
        val_manifest=str(args.val_manifest.resolve()),
        output_dir=str(args.output_dir.resolve()),
        model_name=args.model_name,
        use_lora=not args.no_lora,
        resume_from=str(args.resume_from.resolve()) if args.resume_from else None,
        epochs=args.epochs or default_epochs,
        batch_size=args.batch_size,
        grad_accum_steps=args.grad_accum_steps,
        learning_rate=args.learning_rate or default_lr,
        weight_decay=args.weight_decay,
        seed=args.seed,
        num_workers=args.num_workers,
        image_size=args.image_size,
        mixed_precision=args.mixed_precision,
        teacher_weight=args.teacher_weight,
        unusable_boost=args.unusable_boost,
        wall_boost=args.wall_boost,
        side_domain_weight=args.side_domain_weight,
    )
    best_dir = train(config)
    print(f"Best bundle saved to {best_dir}")


if __name__ == "__main__":
    main()
