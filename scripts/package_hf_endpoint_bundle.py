from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Package a trained student bundle for Hugging Face Inference Endpoints."
    )
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    return parser.parse_args()


def copy_tree_contents(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        target = destination / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def main() -> None:
    args = parse_args()
    model_dir = args.model_dir.resolve()
    outdir = args.outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    copy_tree_contents(model_dir, outdir)
    copy_tree_contents((Path("deploy") / "hf_endpoint").resolve(), outdir)
    copy_tree_contents(Path("student").resolve(), outdir / "student")
    print(f"Packaged endpoint bundle at {outdir}")


if __name__ == "__main__":
    main()
