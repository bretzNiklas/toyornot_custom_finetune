from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from student.constants import CORE_MEDIA, HUMAN_SOURCES, SIDE_MEDIA, TEACHER_SOURCE, is_score_domain, score_bucket
from student.io import choose_stratified, load_jsonl, resolve_absolute_path, write_json, write_jsonl


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build student-training manifests from the merged training pool."
    )
    parser.add_argument(
        "--training-pool",
        type=Path,
        default=Path("exports/final/training_pool_v1.jsonl"),
        help="Merged training pool JSONL.",
    )
    parser.add_argument(
        "--outdir",
        type=Path,
        default=Path("exports/student/v1"),
        help="Output directory for student-training manifests.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Deterministic split seed.",
    )
    parser.add_argument(
        "--val-ratio",
        type=float,
        default=0.15,
        help="Validation ratio over human-quality rows.",
    )
    parser.add_argument(
        "--test-ratio",
        type=float,
        default=0.15,
        help="Test ratio over human-quality rows.",
    )
    return parser.parse_args()


def enrich_rows(rows: list[dict], root: Path) -> list[dict]:
    enriched: list[dict] = []
    for row in rows:
        item = dict(row)
        item["absolute_path"] = resolve_absolute_path(item, root)
        item["score_domain"] = is_score_domain(item)
        item["score_bucket"] = score_bucket(item.get("overall_score"))
        item["human_quality"] = item.get("label_source") in HUMAN_SOURCES
        enriched.append(item)
    return enriched


def summarize(rows: list[dict], field: str) -> dict[str, int]:
    counts = Counter(str(row.get(field)) for row in rows)
    return dict(sorted(counts.items()))


def build_readme(
    *,
    all_rows: list[dict],
    human_rows: list[dict],
    human_train: list[dict],
    human_val: list[dict],
    human_test: list[dict],
    teacher_rows: list[dict],
    teacher_core: list[dict],
    stage_a_rows: list[dict],
) -> str:
    lines = [
        "# Student V1 Artifacts",
        "",
        "## Snapshot",
        "",
        f"- total merged rows: {len(all_rows)}",
        f"- human-quality rows: {len(human_rows)}",
        f"- teacher rows: {len(teacher_rows)}",
        f"- stage A rows: {len(stage_a_rows)}",
        f"- teacher core score rows: {len(teacher_core)}",
        "",
        "## Human Locked Split",
        "",
        f"- train: {len(human_train)}",
        f"- val: {len(human_val)}",
        f"- test: {len(human_test)}",
        "",
        "## Human Train Mix",
        "",
        f"- usable: {summarize(human_train, 'image_usable')}",
        f"- medium: {summarize([row for row in human_train if row.get('image_usable')], 'medium')}",
        f"- score bucket: {summarize([row for row in human_train if row.get('score_domain')], 'score_bucket')}",
        "",
        "## Locked Eval Mix",
        "",
        f"- val medium: {summarize([row for row in human_val if row.get('image_usable')], 'medium')}",
        f"- test medium: {summarize([row for row in human_test if row.get('image_usable')], 'medium')}",
        "",
        "## Notes",
        "",
        "- Validation and test are human-only and must stay locked.",
        "- Stage A includes all human-train rows plus all teacher rows.",
        "- Score losses should be masked to usable paper/wall rows only.",
        "- Digital and other_or_unclear remain for medium learning, not score learning.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    root = Path.cwd()
    all_rows = enrich_rows(load_jsonl(args.training_pool.resolve()), root)

    human_rows = [row for row in all_rows if row.get("label_source") in HUMAN_SOURCES]
    teacher_rows = [row for row in all_rows if row.get("label_source") == TEACHER_SOURCE]
    teacher_core = [row for row in teacher_rows if row.get("image_usable") and row.get("medium") in CORE_MEDIA]

    val_size = round(len(human_rows) * args.val_ratio)
    test_size = round(len(human_rows) * args.test_ratio)
    if val_size <= 0 or test_size <= 0:
        raise SystemExit("Validation/test ratios are too small for the human-quality pool.")

    human_test = choose_stratified(human_rows, test_size, args.seed)
    human_test_files = {row["file"] for row in human_test}
    remaining = [row for row in human_rows if row["file"] not in human_test_files]
    human_val = choose_stratified(remaining, val_size, args.seed + 1)
    human_val_files = {row["file"] for row in human_val}
    human_train = [
        row for row in human_rows
        if row["file"] not in human_test_files and row["file"] not in human_val_files
    ]

    stage_a_rows = sorted(human_train + teacher_rows, key=lambda row: row["file"])

    outdir = args.outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    write_jsonl(outdir / "human_all_v1.jsonl", sorted(human_rows, key=lambda row: row["file"]))
    write_jsonl(outdir / "human_train_v1.jsonl", sorted(human_train, key=lambda row: row["file"]))
    write_jsonl(outdir / "human_val_locked_v1.jsonl", sorted(human_val, key=lambda row: row["file"]))
    write_jsonl(outdir / "human_test_locked_v1.jsonl", sorted(human_test, key=lambda row: row["file"]))
    write_jsonl(outdir / "teacher_stage_a_weak_v1.jsonl", sorted(teacher_rows, key=lambda row: row["file"]))
    write_jsonl(outdir / "teacher_core_score_v1.jsonl", sorted(teacher_core, key=lambda row: row["file"]))
    write_jsonl(outdir / "stage_a_train_v1.jsonl", stage_a_rows)

    summary = {
        "total_rows": len(all_rows),
        "human_rows": len(human_rows),
        "teacher_rows": len(teacher_rows),
        "teacher_core_score_rows": len(teacher_core),
        "human_train_rows": len(human_train),
        "human_val_rows": len(human_val),
        "human_test_rows": len(human_test),
        "human_medium_counts": summarize([row for row in human_rows if row.get("image_usable")], "medium"),
        "teacher_medium_counts": summarize([row for row in teacher_rows if row.get("image_usable")], "medium"),
    }
    write_json(outdir / "summary.json", summary)
    (outdir / "README.md").write_text(
        build_readme(
            all_rows=all_rows,
            human_rows=human_rows,
            human_train=human_train,
            human_val=human_val,
            human_test=human_test,
            teacher_rows=teacher_rows,
            teacher_core=teacher_core,
            stage_a_rows=stage_a_rows,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"Wrote student artifacts to {outdir}")
    print(f"human_train={len(human_train)}")
    print(f"human_val={len(human_val)}")
    print(f"human_test={len(human_test)}")
    print(f"teacher_rows={len(teacher_rows)}")
    print(f"stage_a_rows={len(stage_a_rows)}")


if __name__ == "__main__":
    main()
