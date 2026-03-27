from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import torch
from accelerate import Accelerator
from torch import nn
from torch.optim import AdamW
from torch.utils.data import DataLoader
from tqdm.auto import tqdm
from transformers import AutoImageProcessor

from .checkpoint import load_student_bundle, save_student_bundle
from .constants import CORE_MEDIA, SCORE_FIELDS
from .data import (
    GraffitiTrainingDataset,
    LabelMaps,
    build_stage_sample_weights,
    collate_training_batch,
    create_eval_transform,
    create_train_transform,
    load_jsonl,
    make_weighted_sampler,
)
from .metrics import build_prediction_record, compute_multitask_metrics, tune_binary_threshold
from .model import GraffitiStudentModel, StudentModelConfig


@dataclass(slots=True)
class TrainingConfig:
    stage: str
    train_manifest: str
    val_manifest: str
    output_dir: str
    model_name: str = "google/vit-base-patch16-224-in21k"
    use_lora: bool = True
    resume_from: str | None = None
    epochs: int = 10
    batch_size: int = 8
    grad_accum_steps: int = 2
    learning_rate: float = 1e-4
    weight_decay: float = 0.01
    seed: int = 42
    num_workers: int = 2
    image_size: int = 224
    mixed_precision: str = "fp16"
    teacher_weight: float = 0.35
    unusable_boost: float = 8.0
    wall_boost: float = 1.5
    side_domain_weight: float = 0.6
    usable_loss_weight: float = 1.0
    medium_loss_weight: float = 0.25
    color_applicable_loss_weight: float = 0.25
    overall_loss_weight: float = 2.0
    rubric_loss_weight: float = 0.5


def set_seed(seed: int) -> None:
    random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def masked_mean(loss_values: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    weighted = loss_values * mask
    denom = mask.sum().clamp_min(1.0)
    return weighted.sum() / denom


def compute_losses(
    outputs: dict[str, torch.Tensor],
    batch: dict[str, torch.Tensor],
    config: TrainingConfig,
) -> tuple[torch.Tensor, dict[str, float]]:
    usable_loss_raw = nn.functional.binary_cross_entropy_with_logits(
        outputs["usable_logits"],
        batch["image_usable"],
        reduction="none",
    )
    usable_loss = (usable_loss_raw * batch["sample_weight"]).mean()

    medium_loss_raw = nn.functional.cross_entropy(
        outputs["medium_logits"],
        batch["medium_target"],
        reduction="none",
        ignore_index=-100,
    )
    medium_loss = masked_mean(medium_loss_raw, batch["medium_mask"] * batch["sample_weight"])

    color_loss_raw = nn.functional.binary_cross_entropy_with_logits(
        outputs["color_applicable_logits"],
        batch["color_applicable"],
        reduction="none",
    )
    color_loss = masked_mean(color_loss_raw, batch["color_applicable_mask"] * batch["sample_weight"])

    overall_loss_raw = nn.functional.huber_loss(
        outputs["overall_score"],
        batch["overall_score"],
        delta=1.0,
        reduction="none",
    )
    overall_loss = masked_mean(overall_loss_raw, batch["overall_mask"] * batch["sample_weight"])

    rubric_total = outputs["overall_score"].new_tensor(0.0)
    components = {
        "usable_loss": usable_loss.detach().item(),
        "medium_loss": medium_loss.detach().item(),
        "color_applicable_loss": color_loss.detach().item(),
        "overall_loss": overall_loss.detach().item(),
    }

    for field in SCORE_FIELDS:
        loss_raw = nn.functional.huber_loss(
            outputs[field],
            batch[field],
            delta=1.0,
            reduction="none",
        )
        field_loss = masked_mean(loss_raw, batch[f"{field}_mask"] * batch["sample_weight"])
        rubric_total = rubric_total + field_loss
        components[f"{field}_loss"] = field_loss.detach().item()

    total_loss = (
        config.usable_loss_weight * usable_loss
        + config.medium_loss_weight * medium_loss
        + config.color_applicable_loss_weight * color_loss
        + config.overall_loss_weight * overall_loss
        + config.rubric_loss_weight * rubric_total
    )
    return total_loss, components


def create_model_and_processor(config: TrainingConfig):
    if config.resume_from:
        device = torch.device("cpu")
        return load_student_bundle(Path(config.resume_from), device)[:2]
    processor = AutoImageProcessor.from_pretrained(config.model_name)
    model = GraffitiStudentModel(
        StudentModelConfig(
            backbone_model_name=config.model_name,
            use_lora=config.use_lora,
        ),
        load_pretrained_backbone=True,
    )
    return model, processor


def build_dataloaders(config: TrainingConfig, processor: Any):
    train_rows = load_jsonl(Path(config.train_manifest))
    val_rows = load_jsonl(Path(config.val_manifest))
    label_maps = LabelMaps.default()

    image_mean = list(getattr(processor, "image_mean", [0.5, 0.5, 0.5]))
    image_std = list(getattr(processor, "image_std", [0.5, 0.5, 0.5]))
    train_transform = create_train_transform(config.image_size, image_mean, image_std)
    eval_transform = create_eval_transform(config.image_size, image_mean, image_std)

    stage_weights = build_stage_sample_weights(
        train_rows,
        stage=config.stage,
        teacher_weight=config.teacher_weight,
        unusable_boost=config.unusable_boost,
        wall_boost=config.wall_boost,
        side_domain_weight=config.side_domain_weight,
    )
    weighted_rows = [dict(row, sample_weight=weight) for row, weight in zip(train_rows, stage_weights)]

    train_dataset = GraffitiTrainingDataset(weighted_rows, transform=train_transform, label_maps=label_maps)
    val_dataset = GraffitiTrainingDataset(val_rows, transform=eval_transform, label_maps=label_maps)

    train_loader = DataLoader(
        train_dataset,
        batch_size=config.batch_size,
        sampler=make_weighted_sampler(stage_weights, config.seed),
        num_workers=config.num_workers,
        pin_memory=True,
        collate_fn=collate_training_batch,
    )
    val_loader = DataLoader(
        val_dataset,
        batch_size=config.batch_size,
        shuffle=False,
        num_workers=config.num_workers,
        pin_memory=True,
        collate_fn=collate_training_batch,
    )
    return train_loader, val_loader, train_rows, val_rows


def evaluate_model(
    model: GraffitiStudentModel,
    loader: DataLoader,
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, float]]:
    model.eval()
    device = next(model.parameters()).device
    probability_records: list[dict[str, Any]] = []
    medium_labels = list(model.student_config.medium_labels)
    row_by_file = {row["file"]: row for row in rows}

    for batch in loader:
        pixel_values = batch["pixel_values"].to(device)
        with torch.inference_mode():
            outputs = model(pixel_values=pixel_values)
        usable_probabilities = torch.sigmoid(outputs["usable_logits"]).detach().cpu().tolist()
        color_probabilities = torch.sigmoid(outputs["color_applicable_logits"]).detach().cpu().tolist()
        medium_predictions = outputs["medium_logits"].argmax(dim=-1).detach().cpu().tolist()
        overall_predictions = outputs["overall_score"].detach().cpu().tolist()
        score_predictions = {field: outputs[field].detach().cpu().tolist() for field in SCORE_FIELDS}

        for index, file_name in enumerate(batch["file"]):
            row = row_by_file[file_name]
            raw_scores = {
                "color_applicable_probability": color_probabilities[index],
                "overall_score": overall_predictions[index],
            }
            for field in SCORE_FIELDS:
                raw_scores[field] = score_predictions[field][index]
            probability_records.append(
                {
                    "row": row,
                    "usable_probability": usable_probabilities[index],
                    "medium_prediction": medium_labels[medium_predictions[index]],
                    "raw_scores": raw_scores,
                }
            )

    usable_targets = [1 if record["row"].get("image_usable") else 0 for record in probability_records]
    usable_probabilities = [record["usable_probability"] for record in probability_records]
    usable_threshold = tune_binary_threshold(usable_targets, usable_probabilities, min_recall=0.95)

    color_candidates = [
        record for record in probability_records
        if record["row"].get("image_usable") and record["row"].get("medium") in CORE_MEDIA
    ]
    if color_candidates:
        color_targets = [1 if record["row"].get("color_harmony") is not None else 0 for record in color_candidates]
        color_probabilities = [record["raw_scores"]["color_applicable_probability"] for record in color_candidates]
        color_threshold = tune_binary_threshold(color_targets, color_probabilities)
    else:
        color_threshold = 0.5

    records = [
        build_prediction_record(
            usable_probability=record["usable_probability"],
            usable_threshold=usable_threshold,
            medium_target=record["row"].get("medium"),
            medium_prediction=record["medium_prediction"],
            score_domain_target=bool(record["row"].get("image_usable")) and record["row"].get("medium") in CORE_MEDIA,
            raw_scores=record["raw_scores"],
            row=record["row"],
            color_threshold=color_threshold,
        )
        for record in probability_records
    ]
    metrics = compute_multitask_metrics(records, usable_threshold=usable_threshold, color_threshold=color_threshold)
    thresholds = {
        "usable": usable_threshold,
        "color_applicable": color_threshold,
    }
    return metrics, thresholds


def train(config: TrainingConfig) -> Path:
    set_seed(config.seed)
    accelerator = Accelerator(
        gradient_accumulation_steps=config.grad_accum_steps,
        mixed_precision=config.mixed_precision,
    )
    model, processor = create_model_and_processor(config)
    train_loader, val_loader, train_rows, val_rows = build_dataloaders(config, processor)
    optimizer = AdamW(model.parameters(), lr=config.learning_rate, weight_decay=config.weight_decay)

    model, optimizer, train_loader, val_loader = accelerator.prepare(model, optimizer, train_loader, val_loader)

    best_score = float("inf")
    best_dir = Path(config.output_dir).resolve() / "best_bundle"
    epoch_history: list[dict[str, Any]] = []

    for epoch_index in range(config.epochs):
        model.train()
        running_loss = 0.0
        progress = tqdm(
            train_loader,
            disable=not accelerator.is_local_main_process,
            desc=f"{config.stage} epoch {epoch_index + 1}/{config.epochs}",
        )
        for step, batch in enumerate(progress, start=1):
            with accelerator.accumulate(model):
                outputs = model(pixel_values=batch["pixel_values"])
                loss, _ = compute_losses(outputs, batch, config)
                accelerator.backward(loss)
                optimizer.step()
                optimizer.zero_grad()
            running_loss += loss.detach().item()
            progress.set_postfix(loss=f"{running_loss / step:.4f}")

        accelerator.wait_for_everyone()
        if accelerator.is_main_process:
            unwrapped = accelerator.unwrap_model(model)
            metrics, thresholds = evaluate_model(unwrapped, val_loader, val_rows)
            epoch_summary = {
                "epoch": epoch_index + 1,
                "train_loss": running_loss / max(len(train_loader), 1),
                "val_metrics": metrics,
                "thresholds": thresholds,
            }
            epoch_history.append(epoch_summary)

            usable_recall = metrics["image_usable"]["recall"]
            penalty = max(0.0, 0.95 - usable_recall) * 10.0
            score = metrics["overall_score_mae"] + penalty
            if score < best_score:
                best_score = score
                save_student_bundle(
                    model=unwrapped,
                    processor=processor,
                    output_dir=best_dir,
                    training_config=asdict(config),
                    metrics={
                        "best_score": best_score,
                        "best_epoch": epoch_index + 1,
                        "val_metrics": metrics,
                        "epoch_history": epoch_history,
                        "train_rows": len(train_rows),
                        "val_rows": len(val_rows),
                    },
                    thresholds=thresholds,
                )

    accelerator.wait_for_everyone()
    return best_dir
