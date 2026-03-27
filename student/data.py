from __future__ import annotations

import base64
import io
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import torch
from PIL import Image
from torch.utils.data import Dataset, WeightedRandomSampler
from torchvision import transforms

from .constants import ALL_MEDIA, CORE_MEDIA, SCORE_FIELDS, SIDE_MEDIA, TEACHER_SOURCE


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
    if medium in SIDE_MEDIA:
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
    if medium in SIDE_MEDIA:
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


def create_train_transform(image_size: int, image_mean: list[float], image_std: list[float]) -> transforms.Compose:
    return transforms.Compose(
        [
            transforms.Resize((image_size, image_size)),
            transforms.ColorJitter(brightness=0.08, contrast=0.08, saturation=0.04, hue=0.02),
            transforms.RandomAffine(degrees=2, translate=(0.02, 0.02), scale=(0.98, 1.02)),
            transforms.ToTensor(),
            transforms.Normalize(mean=image_mean, std=image_std),
        ]
    )


def create_eval_transform(image_size: int, image_mean: list[float], image_std: list[float]) -> transforms.Compose:
    return transforms.Compose(
        [
            transforms.Resize((image_size, image_size)),
            transforms.ToTensor(),
            transforms.Normalize(mean=image_mean, std=image_std),
        ]
    )


def build_stage_sample_weights(
    rows: list[dict[str, Any]],
    *,
    stage: str,
    teacher_weight: float = 0.35,
    unusable_boost: float = 8.0,
    wall_boost: float = 1.5,
    side_domain_weight: float = 0.6,
) -> list[float]:
    weights: list[float] = []
    for row in rows:
        label_source = row.get("label_source")
        if stage == "stage_b" and label_source == TEACHER_SOURCE:
            raise ValueError("Stage B rows must be human-only.")
        weight = teacher_weight if label_source == TEACHER_SOURCE else 1.0
        if not row.get("image_usable"):
            weight *= unusable_boost
        elif row.get("medium") == "wall_piece":
            weight *= wall_boost
        elif row.get("medium") in SIDE_MEDIA:
            weight *= side_domain_weight
        weights.append(weight)
    return weights


def make_weighted_sampler(weights: list[float], seed: int) -> WeightedRandomSampler:
    generator = torch.Generator()
    generator.manual_seed(seed)
    return WeightedRandomSampler(
        weights=torch.tensor(weights, dtype=torch.double),
        num_samples=len(weights),
        replacement=True,
        generator=generator,
    )


@dataclass(slots=True)
class LabelMaps:
    medium_to_index: dict[str, int]
    index_to_medium: dict[int, str]

    @classmethod
    def default(cls) -> "LabelMaps":
        medium_to_index = {label: index for index, label in enumerate(ALL_MEDIA)}
        index_to_medium = {index: label for label, index in medium_to_index.items()}
        return cls(medium_to_index=medium_to_index, index_to_medium=index_to_medium)


class GraffitiTrainingDataset(Dataset):
    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        transform: transforms.Compose,
        label_maps: LabelMaps,
    ) -> None:
        self.rows = rows
        self.transform = transform
        self.label_maps = label_maps

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> dict[str, Any]:
        row = self.rows[index]
        image = Image.open(row["absolute_path"]).convert("RGB")
        pixel_values = self.transform(image)

        usable_target = 1.0 if row.get("image_usable") else 0.0
        medium = row.get("medium")
        medium_target = self.label_maps.medium_to_index[medium] if medium in self.label_maps.medium_to_index else -100
        medium_mask = bool(row.get("image_usable")) and medium in self.label_maps.medium_to_index
        score_mask = bool(row.get("image_usable")) and medium in CORE_MEDIA
        color_mask = score_mask and row.get("color_harmony") is not None

        sample = {
            "file": row["file"],
            "pixel_values": pixel_values,
            "sample_weight": torch.tensor(float(row.get("sample_weight", 1.0)), dtype=torch.float32),
            "image_usable": torch.tensor(usable_target, dtype=torch.float32),
            "medium_target": torch.tensor(medium_target, dtype=torch.long),
            "medium_mask": torch.tensor(1.0 if medium_mask else 0.0, dtype=torch.float32),
            "color_applicable": torch.tensor(1.0 if color_mask else 0.0, dtype=torch.float32),
            "color_applicable_mask": torch.tensor(1.0 if score_mask else 0.0, dtype=torch.float32),
            "overall_score": torch.tensor(float(row.get("overall_score") or 0.0), dtype=torch.float32),
            "overall_mask": torch.tensor(1.0 if score_mask and row.get("overall_score") is not None else 0.0, dtype=torch.float32),
            "medium_label": medium,
            "label_source": row.get("label_source"),
        }

        for field in SCORE_FIELDS:
            sample[field] = torch.tensor(float(row.get(field) or 0.0), dtype=torch.float32)
            mask_value = 1.0 if score_mask and row.get(field) is not None else 0.0
            if field == "color_harmony":
                mask_value = 1.0 if color_mask else 0.0
            sample[f"{field}_mask"] = torch.tensor(mask_value, dtype=torch.float32)
        return sample


def collate_training_batch(batch: list[dict[str, Any]]) -> dict[str, Any]:
    collated: dict[str, Any] = {
        "file": [item["file"] for item in batch],
        "medium_label": [item["medium_label"] for item in batch],
        "label_source": [item["label_source"] for item in batch],
    }
    tensor_keys = [key for key in batch[0] if key not in {"file", "medium_label", "label_source"}]
    for key in tensor_keys:
        collated[key] = torch.stack([item[key] for item in batch])
    return collated


def decode_image_b64(payload: str) -> Image.Image:
    image_bytes = base64.b64decode(payload)
    return Image.open(io.BytesIO(image_bytes)).convert("RGB")
