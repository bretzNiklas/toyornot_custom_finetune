from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from urllib.parse import quote


SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def build_local_file_url(document_root: Path, file_path: Path) -> str:
    relative_path = file_path.relative_to(document_root).as_posix()
    return f"/data/local-files/?d={quote(relative_path)}"


def collect_images(images_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in images_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def make_task(document_root: Path, file_path: Path) -> dict:
    relative_path = file_path.relative_to(document_root).as_posix()
    return {
        "data": {
            "image": build_local_file_url(document_root, file_path),
            "file_name": file_path.name,
            "relative_path": relative_path,
        }
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Label Studio task JSON for local graffiti images."
    )
    parser.add_argument(
        "--document-root",
        type=Path,
        default=Path.cwd(),
        help="Local files document root that Label Studio will serve from.",
    )
    parser.add_argument(
        "--images-dir",
        type=Path,
        default=Path("images"),
        help="Directory containing source images, relative to document root by default.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("label_studio/tasks.json"),
        help="Output JSON file for Label Studio tasks.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional cap on number of tasks to emit.",
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help="Shuffle task order before writing output.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Seed used when --shuffle is enabled.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    document_root = args.document_root.resolve()
    images_dir = args.images_dir
    if not images_dir.is_absolute():
        images_dir = (document_root / images_dir).resolve()

    if not images_dir.exists():
        raise SystemExit(f"Images directory not found: {images_dir}")
    if not images_dir.is_dir():
        raise SystemExit(f"Images path is not a directory: {images_dir}")
    if document_root not in images_dir.parents and document_root != images_dir:
        raise SystemExit(
            f"Images directory must be inside the document root. "
            f"document_root={document_root} images_dir={images_dir}"
        )

    image_paths = collect_images(images_dir)
    if args.shuffle:
        rng = random.Random(args.seed)
        rng.shuffle(image_paths)
    if args.limit is not None:
        image_paths = image_paths[: args.limit]

    tasks = [make_task(document_root, file_path) for file_path in image_paths]

    output_path = args.output
    if not output_path.is_absolute():
        output_path = (document_root / output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    output_path.write_text(json.dumps(tasks, indent=2), encoding="utf-8")

    print(f"Wrote {len(tasks)} tasks to {output_path}")
    print(f"Document root: {document_root}")
    print(f"Images dir: {images_dir}")


if __name__ == "__main__":
    main()
