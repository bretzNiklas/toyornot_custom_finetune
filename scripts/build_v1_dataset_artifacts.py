from __future__ import annotations

import argparse
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path


CORE_MEDIA = {"paper_sketch", "wall_piece"}
SIDE_MEDIA = {"digital", "other_or_unclear"}
TEACHER_ANCHOR_IDS = ["P2", "P4", "P5", "W1", "W3", "W5"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build stable v1 dataset artifacts from current graffiti labels."
    )
    parser.add_argument(
        "--labels",
        type=Path,
        default=Path("exports/current_labels.jsonl"),
        help="Flattened current labels JSONL.",
    )
    parser.add_argument(
        "--anchors",
        type=Path,
        default=Path("exports/anchor_pack_v1.json"),
        help="Anchor pack JSON.",
    )
    parser.add_argument(
        "--outdir",
        type=Path,
        default=Path("exports/v1"),
        help="Output directory for manifests.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for deterministic split creation.",
    )
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_json(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def score_bucket(row: dict) -> str:
    overall = row["overall_score"]
    if overall is None:
        return "unknown"
    if overall <= 4:
        return "low"
    if overall >= 8:
        return "high"
    return "mid"


def group_key(row: dict) -> tuple[str | None, str]:
    return row["medium"], score_bucket(row)


def allocate_counts(rows: list[dict], total: int) -> dict[tuple[str | None, str], int]:
    groups: dict[tuple[str | None, str], list[dict]] = defaultdict(list)
    for row in rows:
        groups[group_key(row)].append(row)

    if total <= 0 or not groups:
        return {key: 0 for key in groups}

    total_rows = len(rows)
    raw_targets: dict[tuple[str | None, str], float] = {
        key: total * (len(items) / total_rows) for key, items in groups.items()
    }
    allocations: dict[tuple[str | None, str], int] = {
        key: min(len(groups[key]), math.floor(target)) for key, target in raw_targets.items()
    }

    assigned = sum(allocations.values())
    remainders = sorted(
        (
            raw_targets[key] - allocations[key],
            len(groups[key]) - allocations[key],
            key,
        )
        for key in groups
    )

    while assigned < total:
        advanced = False
        for _, capacity, key in reversed(remainders):
            if capacity <= 0:
                continue
            if allocations[key] >= len(groups[key]):
                continue
            allocations[key] += 1
            assigned += 1
            advanced = True
            if assigned >= total:
                break
        if not advanced:
            break

    return allocations


def choose_rows(rows: list[dict], total: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    groups: dict[tuple[str | None, str], list[dict]] = defaultdict(list)
    for row in rows:
        groups[group_key(row)].append(row)

    for items in groups.values():
        items.sort(key=lambda row: row["file"])
        rng.shuffle(items)

    allocations = allocate_counts(rows, total)
    chosen: list[dict] = []
    for key in sorted(groups):
        chosen.extend(groups[key][: allocations.get(key, 0)])

    chosen.sort(key=lambda row: (row["medium"] or "", row["file"]))
    return chosen


def with_absolute_paths(rows: list[dict], root: Path) -> list[dict]:
    enriched = []
    for row in rows:
        item = dict(row)
        relative = item.get("relative_path")
        item["absolute_path"] = str((root / relative).resolve()) if relative else None
        enriched.append(item)
    return enriched


def summarize_counts(rows: list[dict], field: str) -> str:
    counts = Counter(row[field] for row in rows)
    return ", ".join(f"{key}={counts[key]}" for key in sorted(counts))


def build_summary(
    all_rows: list[dict],
    usable_rows: list[dict],
    core_rows: list[dict],
    core_strong_rows: list[dict],
    low_conf_review: list[dict],
    side_domain_rows: list[dict],
    val_rows: list[dict],
    test_rows: list[dict],
    train_rows: list[dict],
    teacher_anchors: list[dict],
) -> str:
    lines = [
        "# V1 Dataset Artifacts",
        "",
        "## Snapshot",
        "",
        f"- labeled rows: {len(all_rows)}",
        f"- usable rows: {len(usable_rows)}",
        f"- core usable rows (paper + wall): {len(core_rows)}",
        f"- core strong rows (paper + wall, medium/high confidence): {len(core_strong_rows)}",
        f"- side-domain usable rows (digital + unclear): {len(side_domain_rows)}",
        f"- low-confidence core review queue: {len(low_conf_review)}",
        "",
        "## Splits",
        "",
        f"- train seed: {len(train_rows)}",
        f"- validation locked: {len(val_rows)}",
        f"- test locked: {len(test_rows)}",
        f"- teacher prompt anchors: {len(teacher_anchors)}",
        "",
        "## Train Seed Mix",
        "",
        f"- medium: {summarize_counts(train_rows, 'medium')}",
        f"- piece_type: {summarize_counts(train_rows, 'piece_type')}",
        "",
        "## Validation Mix",
        "",
        f"- medium: {summarize_counts(val_rows, 'medium')}",
        f"- score_bucket: {summarize_counts(val_rows, 'score_bucket')}",
        "",
        "## Test Mix",
        "",
        f"- medium: {summarize_counts(test_rows, 'medium')}",
        f"- score_bucket: {summarize_counts(test_rows, 'score_bucket')}",
        "",
        "## How To Use",
        "",
        "- Keep validation and test locked. Do not use them as prompt examples or relabel them casually.",
        "- Use the teacher prompt anchors only for in-context calibration.",
        "- Use the train seed for a first human-only baseline or for teacher-vs-human comparisons.",
        "- Review the low-confidence queue before folding those labels into training.",
        "- Treat the side-domain file as optional for v1. It is better for later expansion than for the first core model.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    root = Path.cwd()

    all_rows = load_jsonl(args.labels.resolve())
    anchor_pack = load_json(args.anchors.resolve())
    anchor_by_id = {item["anchor_id"]: item for item in anchor_pack}
    anchor_files = {item["file"] for item in anchor_pack}

    usable_rows = [row for row in all_rows if row["image_usable"]]
    core_rows = [row for row in usable_rows if row["medium"] in CORE_MEDIA]
    side_domain_rows = [row for row in usable_rows if row["medium"] in SIDE_MEDIA]
    core_strong_rows = [row for row in core_rows if row["confidence"] in {"medium", "high"}]
    low_conf_review = [row for row in core_rows if row["confidence"] == "low"]

    candidate_rows = [row for row in core_strong_rows if row["file"] not in anchor_files]
    candidate_count = len(candidate_rows)
    val_size = max(12, round(candidate_count * 0.10))
    test_size = max(18, round(candidate_count * 0.15))
    if val_size + test_size >= candidate_count:
        raise SystemExit("Not enough candidate rows to create locked validation and test splits.")

    test_rows = choose_rows(candidate_rows, test_size, args.seed)
    test_files = {row["file"] for row in test_rows}
    remaining_for_val = [row for row in candidate_rows if row["file"] not in test_files]
    val_rows = choose_rows(remaining_for_val, val_size, args.seed + 1)
    val_files = {row["file"] for row in val_rows}
    train_rows = [
        row for row in core_strong_rows if row["file"] not in test_files and row["file"] not in val_files
    ]

    teacher_anchors = [anchor_by_id[anchor_id] for anchor_id in TEACHER_ANCHOR_IDS]

    outdir = args.outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    enriched_files = {
        "all_usable_v1.jsonl": with_absolute_paths(usable_rows, root),
        "core_all_v1.jsonl": with_absolute_paths(core_rows, root),
        "core_low_confidence_review_v1.jsonl": with_absolute_paths(low_conf_review, root),
        "side_domain_review_v1.jsonl": with_absolute_paths(side_domain_rows, root),
        "core_val_locked_v1.jsonl": with_absolute_paths(val_rows, root),
        "core_test_locked_v1.jsonl": with_absolute_paths(test_rows, root),
        "core_train_seed_v1.jsonl": with_absolute_paths(train_rows, root),
    }

    for name, rows in enriched_files.items():
        rows_with_buckets = []
        for row in rows:
            item = dict(row)
            item["score_bucket"] = score_bucket(item)
            rows_with_buckets.append(item)
        write_jsonl(outdir / name, rows_with_buckets)

    teacher_anchors_enriched = with_absolute_paths(teacher_anchors, root)
    for item in teacher_anchors_enriched:
        item["score_bucket"] = item["bucket"]
    write_json(outdir / "teacher_prompt_anchors_v1.json", teacher_anchors_enriched)

    summary = build_summary(
        all_rows=all_rows,
        usable_rows=usable_rows,
        core_rows=core_rows,
        core_strong_rows=core_strong_rows,
        low_conf_review=low_conf_review,
        side_domain_rows=side_domain_rows,
        val_rows=[{**row, "score_bucket": score_bucket(row)} for row in val_rows],
        test_rows=[{**row, "score_bucket": score_bucket(row)} for row in test_rows],
        train_rows=train_rows,
        teacher_anchors=teacher_anchors_enriched,
    )
    (outdir / "README.md").write_text(summary, encoding="utf-8")

    print(f"Wrote dataset artifacts to {outdir}")
    print(f"usable_rows={len(usable_rows)}")
    print(f"core_rows={len(core_rows)}")
    print(f"core_strong_rows={len(core_strong_rows)}")
    print(f"low_conf_review={len(low_conf_review)}")
    print(f"side_domain_rows={len(side_domain_rows)}")
    print(f"train_seed={len(train_rows)}")
    print(f"val_locked={len(val_rows)}")
    print(f"test_locked={len(test_rows)}")
    print(f"teacher_prompt_anchors={len(teacher_anchors_enriched)}")


if __name__ == "__main__":
    main()
