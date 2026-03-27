from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


FIELDS = [
    "legibility",
    "letter_structure",
    "line_quality",
    "composition",
    "color_harmony",
    "originality",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit current graffiti labels for drift, imbalance, and suspicious score combinations."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("exports/current_labels.jsonl"),
        help="Path to flattened label JSONL.",
    )
    return parser.parse_args()


def load_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def fmt_counter(counter: Counter) -> str:
    return ", ".join(f"{key}={value}" for key, value in counter.items())


def medium_summary(rows: list[dict]) -> list[str]:
    lines = []
    for medium in ["paper_sketch", "wall_piece", "digital", "other_or_unclear", None]:
        subset = [row for row in rows if row["medium"] == medium and row["image_usable"]]
        if not subset:
            continue
        lines.append(f"\nMEDIUM {medium} n={len(subset)}")
        for field in FIELDS:
            values = [row[field] for row in subset if row[field] is not None]
            if not values:
                continue
            mean = sum(values) / len(values)
            lines.append(
                f"  {field}: mean={mean:.2f} min={min(values)} max={max(values)} n={len(values)}"
            )
    return lines


def flag_rows(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    return [
        (
            "low_legibility_high_composition",
            [
                row
                for row in rows
                if row["legibility"] is not None
                and row["legibility"] <= 3
                and (row["composition"] or -1) >= 8
            ],
        ),
        (
            "high_originality_low_structure",
            [
                row
                for row in rows
                if row["originality"] is not None
                and row["originality"] >= 8
                and (row["letter_structure"] or 99) <= 3
            ],
        ),
        (
            "high_legibility_low_structure",
            [
                row
                for row in rows
                if row["legibility"] is not None
                and row["legibility"] >= 8
                and (row["letter_structure"] or 99) <= 3
            ],
        ),
        (
            "character_should_be_re_labeled_out_of_scope",
            [row for row in rows if row["piece_type"] == "character"],
        ),
    ]


def row_brief(row: dict) -> str:
    score_parts = " ".join(
        f"{abbr}={row[field]}"
        for abbr, field in [
            ("L", "legibility"),
            ("S", "letter_structure"),
            ("Q", "line_quality"),
            ("C", "composition"),
            ("H", "color_harmony"),
            ("O", "originality"),
        ]
    )
    return (
        f"{row['file']} medium={row['medium']} type={row['piece_type']} "
        f"confidence={row['confidence']} {score_parts}"
    )


def main() -> None:
    args = parse_args()
    rows = load_rows(args.input.resolve())

    print(f"rows={len(rows)}")
    print(f"usable={sum(row['image_usable'] for row in rows)}")
    print(f"medium_counts={fmt_counter(Counter(row['medium'] for row in rows))}")
    print(f"piece_type_counts={fmt_counter(Counter(row['piece_type'] for row in rows))}")
    print(f"confidence_counts={fmt_counter(Counter(row['confidence'] for row in rows))}")

    for line in medium_summary(rows):
        print(line)

    for name, flagged in flag_rows(rows):
        print(f"\n{name} count={len(flagged)}")
        for row in flagged:
            print(f"  {row_brief(row)}")


if __name__ == "__main__":
    main()
