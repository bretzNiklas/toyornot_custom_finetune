from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge original human labels, teacher predictions, and teacher-review corrections."
    )
    parser.add_argument(
        "--human",
        type=Path,
        default=Path("exports/current_labels.jsonl"),
        help="Original human-labeled JSONL.",
    )
    parser.add_argument(
        "--teacher",
        type=Path,
        default=Path("exports/openrouter/teacher_full_predictions_lite_tuned.jsonl"),
        help="Teacher full prediction JSONL.",
    )
    parser.add_argument(
        "--reviewed",
        type=Path,
        required=True,
        help="Flattened reviewed Label Studio export JSONL for the teacher-review project.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("exports/final/training_pool_v1.jsonl"),
        help="Merged output JSONL.",
    )
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def tagged(row: dict, source: str) -> dict:
    item = dict(row)
    item["label_source"] = source
    return item


def main() -> None:
    args = parse_args()
    human_rows = load_jsonl(args.human.resolve())
    teacher_rows = load_jsonl(args.teacher.resolve())
    reviewed_rows = load_jsonl(args.reviewed.resolve())

    human_by_file = {row["file"]: tagged(row, "human_original") for row in human_rows}
    teacher_by_file = {row["file"]: tagged(row, "teacher_lite_tuned") for row in teacher_rows}
    reviewed_by_file = {row["file"]: tagged(row, "human_reviewed_teacher") for row in reviewed_rows}

    merged = dict(teacher_by_file)
    merged.update(reviewed_by_file)
    merged.update(human_by_file)

    merged_rows = [merged[file_name] for file_name in sorted(merged)]
    write_jsonl(args.output.resolve(), merged_rows)

    print(f"Wrote merged training pool to {args.output.resolve()}")
    print(f"human_original={len(human_by_file)}")
    print(f"teacher_full={len(teacher_by_file)}")
    print(f"reviewed_teacher={len(reviewed_by_file)}")
    print(f"final_rows={len(merged_rows)}")


if __name__ == "__main__":
    main()
