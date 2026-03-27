from __future__ import annotations

import argparse
import json
import math
import random
from collections import defaultdict
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a 50-image teacher pilot manifest from existing human-labeled splits."
    )
    parser.add_argument(
        "--val",
        type=Path,
        default=Path("exports/v1/core_val_locked_v1.jsonl"),
        help="Locked validation split JSONL.",
    )
    parser.add_argument(
        "--test",
        type=Path,
        default=Path("exports/v1/core_test_locked_v1.jsonl"),
        help="Locked test split JSONL.",
    )
    parser.add_argument(
        "--train",
        type=Path,
        default=Path("exports/v1/core_train_seed_v1.jsonl"),
        help="Seed training split JSONL.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("exports/v1/teacher_pilot_50_v1.jsonl"),
        help="Output pilot manifest JSONL.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for deterministic selection.",
    )
    parser.add_argument(
        "--extra-train",
        type=int,
        default=10,
        help="How many extra train rows to add on top of locked eval rows.",
    )
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def group_key(row: dict) -> tuple[str | None, str]:
    return row.get("medium"), row.get("score_bucket")


def choose_stratified(rows: list[dict], total: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    groups: dict[tuple[str | None, str], list[dict]] = defaultdict(list)
    for row in rows:
        groups[group_key(row)].append(row)

    for items in groups.values():
        items.sort(key=lambda row: row["file"])
        rng.shuffle(items)

    raw_targets = {
        key: total * (len(items) / len(rows))
        for key, items in groups.items()
    }
    allocations = {
        key: min(len(groups[key]), math.floor(target))
        for key, target in raw_targets.items()
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
        for _, remaining_capacity, key in reversed(remainders):
            if remaining_capacity <= 0:
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

    chosen: list[dict] = []
    for key in sorted(groups):
        chosen.extend(groups[key][: allocations.get(key, 0)])

    chosen.sort(key=lambda row: (row.get("medium") or "", row["file"]))
    return chosen


def main() -> None:
    args = parse_args()
    val_rows = load_jsonl(args.val.resolve())
    test_rows = load_jsonl(args.test.resolve())
    train_rows = load_jsonl(args.train.resolve())

    locked_eval = []
    for row in val_rows:
        item = dict(row)
        item["pilot_group"] = "locked_eval"
        item["locked_split"] = "val"
        locked_eval.append(item)
    for row in test_rows:
        item = dict(row)
        item["pilot_group"] = "locked_eval"
        item["locked_split"] = "test"
        locked_eval.append(item)

    locked_files = {row["file"] for row in locked_eval}
    eligible_train = [row for row in train_rows if row["file"] not in locked_files]
    extra_rows = choose_stratified(eligible_train, args.extra_train, args.seed)
    for row in extra_rows:
        row["pilot_group"] = "pilot_only"
        row["locked_split"] = None

    pilot_rows = locked_eval + extra_rows
    pilot_rows.sort(key=lambda row: (row["pilot_group"], row.get("medium") or "", row["file"]))
    write_jsonl(args.output.resolve(), pilot_rows)

    print(f"Wrote {len(pilot_rows)} pilot rows to {args.output.resolve()}")
    print(f"locked_eval={len(locked_eval)}")
    print(f"pilot_only={len(extra_rows)}")


if __name__ == "__main__":
    main()
