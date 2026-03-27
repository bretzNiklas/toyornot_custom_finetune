from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


PRESETS = [
    "vit_base_384",
    "dinov2_base_224",
    "convnextv2_tiny_224",
    "efficientnet_b0_224",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the full student benchmark suite sequentially."
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=Path("runs/benchmarks"),
        help="Where experiment outputs should be written.",
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=Path("exports/student/v1"),
        help="Directory containing student manifests.",
    )
    parser.add_argument("--mixed-precision", default="fp16")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--stage-a-epochs", type=int, default=10)
    parser.add_argument("--stage-b-epochs", type=int, default=5)
    parser.add_argument(
        "--include-baseline",
        action="store_true",
        help="Also run vit_base_224 as a baseline under the same runner.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue to the next preset if one run fails.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    presets = list(PRESETS)
    if args.include_baseline:
        presets.insert(0, "vit_base_224")

    python_exe = Path(sys.executable).resolve()
    script = Path("scripts/run_student_experiment.py").resolve()

    summary: list[dict[str, object]] = []
    for preset in presets:
        command = [
            str(python_exe),
            str(script),
            "--preset",
            preset,
            "--name",
            preset,
            "--runs-dir",
            str(args.runs_dir.resolve()),
            "--artifacts-dir",
            str(args.artifacts_dir.resolve()),
            "--mixed-precision",
            args.mixed_precision,
            "--seed",
            str(args.seed),
            "--stage-a-epochs",
            str(args.stage_a_epochs),
            "--stage-b-epochs",
            str(args.stage_b_epochs),
        ]
        print(f"\n=== Running {preset} ===")
        result = subprocess.run(command)
        summary.append({"preset": preset, "return_code": result.returncode})
        if result.returncode != 0 and not args.continue_on_error:
            break

    summary_path = args.runs_dir.resolve() / "suite_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    print(f"\nWrote suite summary to {summary_path}")

    failed = [item for item in summary if item["return_code"] != 0]
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
