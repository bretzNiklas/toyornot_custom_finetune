from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


NUMERIC_FIELDS = [
    "legibility",
    "letter_structure",
    "line_quality",
    "composition",
    "color_harmony",
    "originality",
    "overall_score",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate teacher predictions against human-labeled pilot data."
    )
    parser.add_argument(
        "--human",
        type=Path,
        default=Path("exports/v1/teacher_pilot_50_v1.jsonl"),
        help="Human-labeled pilot manifest JSONL.",
    )
    parser.add_argument(
        "--predictions",
        type=Path,
        default=Path("exports/openrouter/teacher_pilot_predictions.jsonl"),
        help="Teacher predictions JSONL.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("exports/openrouter/teacher_pilot_report.md"),
        help="Markdown report path.",
    )
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def score_bucket(score: int | None) -> str | None:
    if score is None:
        return None
    if score <= 4:
        return "low"
    if score >= 8:
        return "high"
    return "mid"


def accuracy(pairs: list[tuple[object, object]]) -> float | None:
    if not pairs:
        return None
    correct = sum(1 for truth, pred in pairs if truth == pred)
    return correct / len(pairs)


def mae(pairs: list[tuple[int, int]]) -> float | None:
    if not pairs:
        return None
    return sum(abs(truth - pred) for truth, pred in pairs) / len(pairs)


def fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def fmt_float(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def subset_report(name: str, human_rows: list[dict], pred_by_file: dict[str, dict]) -> list[str]:
    matched = [(row, pred_by_file[row["file"]]) for row in human_rows if row["file"] in pred_by_file]
    coverage = len(matched) / len(human_rows) if human_rows else 0.0

    usable_pairs = [(h["image_usable"], p["image_usable"]) for h, p in matched]
    medium_pairs = [
        (h["medium"], p["medium"])
        for h, p in matched
        if h["image_usable"] and p["image_usable"]
    ]
    piece_pairs = [
        (h["piece_type"], p["piece_type"])
        for h, p in matched
        if h["image_usable"] and p["image_usable"]
    ]
    bucket_pairs = [
        (score_bucket(h["overall_score"]), score_bucket(p["overall_score"]))
        for h, p in matched
        if h["image_usable"] and p["image_usable"]
    ]

    lines = [
        f"## {name}",
        "",
        f"- human rows: {len(human_rows)}",
        f"- matched predictions: {len(matched)}",
        f"- coverage: {coverage * 100:.1f}%",
        f"- image usable accuracy: {fmt_pct(accuracy(usable_pairs))}",
        f"- medium accuracy: {fmt_pct(accuracy(medium_pairs))}",
        f"- piece type accuracy: {fmt_pct(accuracy(piece_pairs))}",
        f"- overall bucket accuracy: {fmt_pct(accuracy(bucket_pairs))}",
        "",
        "### Numeric MAE",
        "",
    ]

    for field in NUMERIC_FIELDS:
        pairs = []
        for human_row, pred_row in matched:
            truth = human_row.get(field)
            pred = pred_row.get(field)
            if truth is None or pred is None:
                continue
            pairs.append((truth, pred))
        lines.append(f"- {field}: {fmt_float(mae(pairs))}")

    disagreements = []
    for human_row, pred_row in matched:
        if human_row.get("overall_score") is None or pred_row.get("overall_score") is None:
            continue
        delta = abs(human_row["overall_score"] - pred_row["overall_score"])
        disagreements.append(
            {
                "file": human_row["file"],
                "medium": human_row["medium"],
                "human_overall": human_row["overall_score"],
                "pred_overall": pred_row["overall_score"],
                "delta": delta,
                "human_piece_type": human_row["piece_type"],
                "pred_piece_type": pred_row["piece_type"],
            }
        )

    disagreements.sort(key=lambda item: (-item["delta"], item["file"]))
    lines.extend(["", "### Largest Overall Disagreements", ""])
    for item in disagreements[:10]:
        lines.append(
            f"- {item['file']}: delta={item['delta']} medium={item['medium']} "
            f"human={item['human_overall']} pred={item['pred_overall']} "
            f"human_type={item['human_piece_type']} pred_type={item['pred_piece_type']}"
        )

    return lines


def overall_cost_rows(pred_rows: list[dict]) -> list[str]:
    total_cost = sum(float(row.get("cost_usd") or 0.0) for row in pred_rows)
    avg_cost = total_cost / len(pred_rows) if pred_rows else 0.0
    prompt_tokens = sum(int(row.get("prompt_tokens") or 0) for row in pred_rows)
    completion_tokens = sum(int(row.get("completion_tokens") or 0) for row in pred_rows)
    medium_counts = Counter(row.get("medium") for row in pred_rows)
    medium_mix = ", ".join(
        f"{key}={medium_counts[key]}" for key in sorted(medium_counts, key=lambda value: str(value))
    )
    return [
        "## Teacher Run",
        "",
        f"- predictions written: {len(pred_rows)}",
        f"- total cost usd: {total_cost:.4f}",
        f"- average cost per image usd: {avg_cost:.4f}",
        f"- prompt tokens: {prompt_tokens}",
        f"- completion tokens: {completion_tokens}",
        f"- predicted medium mix: {medium_mix}",
        "",
    ]


def main() -> None:
    human_rows = load_jsonl(args.human.resolve())
    pred_rows = load_jsonl(args.predictions.resolve())
    pred_by_file = {row["file"]: row for row in pred_rows}

    locked_eval_rows = [row for row in human_rows if row.get("pilot_group") == "locked_eval"]
    pilot_only_rows = [row for row in human_rows if row.get("pilot_group") == "pilot_only"]

    lines = ["# Teacher Pilot Evaluation", ""]
    lines.extend(overall_cost_rows(pred_rows))
    lines.extend(subset_report("All Pilot Rows", human_rows, pred_by_file))
    lines.extend([""])
    lines.extend(subset_report("Locked Eval Rows", locked_eval_rows, pred_by_file))
    lines.extend([""])
    lines.extend(subset_report("Pilot-Only Rows", pilot_only_rows, pred_by_file))

    args.report.resolve().parent.mkdir(parents=True, exist_ok=True)
    args.report.resolve().write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote report to {args.report.resolve()}")


if __name__ == "__main__":
    args = parse_args()
    main()
