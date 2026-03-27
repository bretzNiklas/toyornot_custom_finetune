from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def resolve_absolute_path(row: dict[str, Any], root: Path) -> str:
    absolute_path = row.get("absolute_path")
    if absolute_path:
        return str(Path(absolute_path).resolve())
    relative_path = row.get("relative_path")
    if not relative_path:
        raise ValueError(f"Row {row.get('file')} is missing both absolute_path and relative_path.")
    return str((root / relative_path).resolve())


def band_label(row: dict[str, Any]) -> str:
    if not row.get("image_usable"):
        return "unusable"
    medium = row.get("medium")
    if medium in {"digital", "other_or_unclear"}:
        return f"side::{medium}"
    score = row.get("overall_score")
    if score is None:
        return f"core::{medium}::unknown"
    if score <= 3:
        bucket = "low"
    elif score <= 6:
        bucket = "mid"
    else:
        bucket = "high"
    return f"core::{medium}::{bucket}"


def stratify_key(row: dict[str, Any]) -> str:
    if not row.get("image_usable"):
        return "gate::unusable"
    medium = row.get("medium")
    if medium in {"digital", "other_or_unclear"}:
        return f"usable::{medium}"
    return f"usable::{band_label(row)}"


def allocate_counts(groups: dict[str, list[dict[str, Any]]], target_total: int) -> dict[str, int]:
    if target_total <= 0:
        return {key: 0 for key in groups}
    total_rows = sum(len(items) for items in groups.values())
    raw = {
        key: target_total * (len(items) / total_rows)
        for key, items in groups.items()
    }
    allocated = {
        key: min(len(groups[key]), int(raw_target))
        for key, raw_target in raw.items()
    }
    assigned = sum(allocated.values())
    ranked = sorted(
        (
            raw[key] - allocated[key],
            len(groups[key]) - allocated[key],
            key,
        )
        for key in groups
    )
    while assigned < target_total:
        advanced = False
        for _, remaining_capacity, key in reversed(ranked):
            if remaining_capacity <= 0 or allocated[key] >= len(groups[key]):
                continue
            allocated[key] += 1
            assigned += 1
            advanced = True
            if assigned >= target_total:
                break
        if not advanced:
            break
    return allocated


def choose_stratified(rows: list[dict[str, Any]], total: int, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(stratify_key(row), []).append(row)
    for items in groups.values():
        items.sort(key=lambda item: item["file"])
        rng.shuffle(items)
    allocations = allocate_counts(groups, total)
    chosen: list[dict[str, Any]] = []
    for key in sorted(groups):
        chosen.extend(groups[key][: allocations.get(key, 0)])
    chosen.sort(key=lambda item: item["file"])
    return chosen
