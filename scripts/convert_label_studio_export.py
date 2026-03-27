from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flatten Label Studio graffiti annotations into JSONL."
    )
    parser.add_argument("input", type=Path, help="Label Studio JSON export file.")
    parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        default=Path("exports/labels.jsonl"),
        help="Output JSONL path.",
    )
    return parser.parse_args()


def latest_annotation(task: dict[str, Any]) -> dict[str, Any] | None:
    annotations = task.get("annotations") or task.get("completions") or []
    if not annotations:
        return None
    return max(
        annotations,
        key=lambda item: item.get("updated_at")
        or item.get("created_at")
        or item.get("id")
        or 0,
    )


def extract_result_map(annotation: dict[str, Any]) -> dict[str, Any]:
    result_map: dict[str, Any] = {}
    for item in annotation.get("result", []):
        result_map[item.get("from_name")] = item.get("value", {})
    return result_map


def choice_value(result_map: dict[str, Any], name: str) -> str | None:
    value = result_map.get(name)
    if not value:
        return None
    choices = value.get("choices") or []
    return choices[0] if choices else None


def text_value(result_map: dict[str, Any], name: str) -> str | None:
    value = result_map.get(name)
    if not value:
        return None
    text = value.get("text") or []
    if not text:
        return None
    return " ".join(part.strip() for part in text if part and part.strip()) or None


def rating_value(result_map: dict[str, Any], name: str) -> int | None:
    value = result_map.get(name)
    if not value:
        return None
    rating = value.get("rating")
    return int(rating) if rating is not None else None


def normalize_medium(value: str | None) -> str | None:
    mapping = {
        "Paper sketch": "paper_sketch",
        "Wall piece": "wall_piece",
        "Digital": "digital",
        "Other / unclear": "other_or_unclear",
    }
    return mapping.get(value)


def normalize_piece_type(value: str | None) -> str | None:
    mapping = {
        "Tag": "tag",
        "Throwie": "throwie",
        "Straight letter": "straight_letter",
        "Piece": "piece",
        "Wildstyle": "wildstyle",
        "Character": "character",
        "Mixed": "mixed",
        "Other": "other",
    }
    return mapping.get(value)


def normalize_confidence(value: str | None) -> str | None:
    if value is None:
        return None
    return value.lower()


def unusable_reason_to_note(reason: str | None, note: str | None) -> str | None:
    if reason and note:
        return f"{reason}: {note}"
    return reason or note


def flatten_task(task: dict[str, Any]) -> dict[str, Any] | None:
    annotation = latest_annotation(task)
    if annotation is None:
        return None

    data = task.get("data", {})
    result_map = extract_result_map(annotation)
    usable_choice = choice_value(result_map, "image_usable")
    is_usable = usable_choice == "Usable"

    exclude_reason = None
    notes = text_value(result_map, "notes")
    if not is_usable:
        exclude_reason = choice_value(result_map, "unusable_reason")
        notes = unusable_reason_to_note(
            exclude_reason,
            text_value(result_map, "notes_unusable"),
        )

    color_applicable = choice_value(result_map, "color_applicable")
    color_harmony = rating_value(result_map, "color_harmony")
    if color_applicable == "Not applicable":
        color_harmony = None

    flattened = {
        "task_id": task.get("id"),
        "annotation_id": annotation.get("id"),
        "file": data.get("file_name"),
        "relative_path": data.get("relative_path"),
        "image": data.get("image"),
        "image_usable": is_usable,
        "exclude_reason": exclude_reason,
        "medium": normalize_medium(choice_value(result_map, "medium")) if is_usable else None,
        "piece_type": normalize_piece_type(choice_value(result_map, "piece_type")) if is_usable else None,
        "legibility": rating_value(result_map, "legibility") if is_usable else None,
        "letter_structure": rating_value(result_map, "letter_structure") if is_usable else None,
        "line_quality": rating_value(result_map, "line_quality") if is_usable else None,
        "composition": rating_value(result_map, "composition") if is_usable else None,
        "color_harmony": color_harmony if is_usable else None,
        "originality": rating_value(result_map, "originality") if is_usable else None,
        "confidence": normalize_confidence(choice_value(result_map, "confidence")) if is_usable else "low",
        "notes": notes,
        "label_studio_created_at": annotation.get("created_at"),
        "label_studio_updated_at": annotation.get("updated_at"),
    }

    usable_scores = [
        flattened["legibility"],
        flattened["letter_structure"],
        flattened["line_quality"],
        flattened["composition"],
        flattened["originality"],
    ]
    if flattened["color_harmony"] is not None:
        usable_scores.append(flattened["color_harmony"])

    flattened["overall_score"] = (
        round(sum(usable_scores) / len(usable_scores)) if is_usable and usable_scores else None
    )
    return flattened


def main() -> None:
    args = parse_args()
    raw = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit("Expected Label Studio export JSON to be a list of tasks.")

    rows = [row for task in raw if (row := flatten_task(task)) is not None]

    output_path = args.output.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")

    print(f"Wrote {len(rows)} labeled rows to {output_path}")


if __name__ == "__main__":
    main()
