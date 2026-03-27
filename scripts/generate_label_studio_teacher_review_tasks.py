from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import quote


RATING_FIELDS = [
    "legibility",
    "letter_structure",
    "line_quality",
    "composition",
    "color_harmony",
    "originality",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Label Studio tasks with teacher predictions prefilled for review."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("exports/openrouter/teacher_full_review_queue_lite_tuned.jsonl"),
        help="Teacher review queue JSONL.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("label_studio/teacher_review_tasks.json"),
        help="Output task JSON.",
    )
    parser.add_argument(
        "--document-root",
        type=Path,
        default=Path.cwd(),
        help="Label Studio local file document root.",
    )
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_local_file_url(document_root: Path, relative_path: str) -> str:
    relative = Path(relative_path).relative_to(document_root.relative_to(document_root))
    return f"/data/local-files/?d={quote(relative.as_posix())}"


def choice_result(name: str, to_name: str, value: str) -> dict:
    return {
        "from_name": name,
        "to_name": to_name,
        "type": "choices",
        "value": {"choices": [value]},
    }


def rating_result(name: str, to_name: str, value: int) -> dict:
    return {
        "from_name": name,
        "to_name": to_name,
        "type": "rating",
        "value": {"rating": value},
    }


def textarea_result(name: str, to_name: str, value: str) -> dict:
    return {
        "from_name": name,
        "to_name": to_name,
        "type": "textarea",
        "value": {"text": [value]},
    }


def medium_to_label(value: str | None) -> str | None:
    mapping = {
        "paper_sketch": "Paper sketch",
        "wall_piece": "Wall piece",
        "digital": "Digital",
        "other_or_unclear": "Other / unclear",
    }
    return mapping.get(value)


def piece_type_to_label(value: str | None) -> str | None:
    mapping = {
        "tag": "Tag",
        "throwie": "Throwie",
        "straight_letter": "Straight letter",
        "piece": "Piece",
        "wildstyle": "Wildstyle",
        "mixed": "Mixed",
        "other": "Other",
    }
    return mapping.get(value)


def confidence_to_label(value: str | None) -> str | None:
    mapping = {"low": "Low", "medium": "Medium", "high": "High"}
    return mapping.get(value)


def unusable_reason_label(row: dict) -> str:
    exclude_reason = row.get("exclude_reason")
    mapping = {
        "Blurry": "Blurry",
        "Too dark": "Too dark",
        "Cropped / partial": "Cropped / partial",
        "Not graffiti": "Not graffiti",
        "Character-only / out of scope": "Character-only / out of scope",
        "Corrupt file": "Corrupt file",
        "Other": "Other",
    }
    if exclude_reason in mapping:
        return mapping[exclude_reason]
    return "Other"


def prediction_result(row: dict) -> list[dict]:
    to_name = "image"
    result: list[dict] = []

    if row["image_usable"]:
        result.append(choice_result("image_usable", to_name, "Usable"))
        medium_label = medium_to_label(row.get("medium"))
        if medium_label:
            result.append(choice_result("medium", to_name, medium_label))
        piece_type_label = piece_type_to_label(row.get("piece_type"))
        if piece_type_label:
            result.append(choice_result("piece_type", to_name, piece_type_label))
        for field in RATING_FIELDS:
            value = row.get(field)
            if field == "color_harmony":
                if value is None:
                    result.append(choice_result("color_applicable", to_name, "Not applicable"))
                else:
                    result.append(choice_result("color_applicable", to_name, "Applicable"))
                    result.append(rating_result(field, to_name, value))
                continue
            if value is not None:
                result.append(rating_result(field, to_name, value))
        confidence_label = confidence_to_label(row.get("confidence"))
        if confidence_label:
            result.append(choice_result("confidence", to_name, confidence_label))
        note_parts = []
        if row.get("notes"):
            note_parts.append(f"Teacher note: {row['notes']}")
        if row.get("review_reasons"):
            note_parts.append("Review reasons: " + ", ".join(row["review_reasons"]))
        if note_parts:
            result.append(textarea_result("notes", to_name, " | ".join(note_parts)))
    else:
        result.append(choice_result("image_usable", to_name, "Unusable"))
        result.append(choice_result("unusable_reason", to_name, unusable_reason_label(row)))
        note_parts = []
        if row.get("notes"):
            note_parts.append(f"Teacher note: {row['notes']}")
        if row.get("review_reasons"):
            note_parts.append("Review reasons: " + ", ".join(row["review_reasons"]))
        if note_parts:
            result.append(textarea_result("notes_unusable", to_name, " | ".join(note_parts)))

    return result


def make_task(document_root: Path, row: dict) -> dict:
    relative_path = row["relative_path"].replace("\\", "/")
    return {
        "data": {
            "image": f"/data/local-files/?d={quote(relative_path)}",
            "file_name": row["file"],
            "relative_path": relative_path,
            "teacher_model": row.get("teacher_model"),
            "teacher_overall_score": row.get("overall_score"),
            "teacher_confidence": row.get("confidence"),
            "review_reasons": ", ".join(row.get("review_reasons", [])),
        },
        "predictions": [
            {
                "model_version": row.get("teacher_model") or "teacher",
                "score": 0.5,
                "result": prediction_result(row),
            }
        ],
    }


def main() -> None:
    args = parse_args()
    document_root = args.document_root.resolve()
    rows = load_jsonl(args.input.resolve())
    tasks = [make_task(document_root, row) for row in rows]

    output_path = args.output
    if not output_path.is_absolute():
        output_path = (document_root / output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(tasks, indent=2), encoding="utf-8")

    print(f"Wrote {len(tasks)} teacher review tasks to {output_path}")


if __name__ == "__main__":
    main()
