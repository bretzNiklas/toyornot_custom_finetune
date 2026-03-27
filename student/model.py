from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from peft import LoraConfig, TaskType, get_peft_model
from torch import nn
from transformers import AutoConfig, AutoModel

from .constants import ALL_MEDIA, SCORE_FIELDS


def infer_hidden_size(backbone_config: Any) -> int:
    hidden_size = getattr(backbone_config, "hidden_size", None)
    if hidden_size is not None:
        return int(hidden_size)
    hidden_sizes = getattr(backbone_config, "hidden_sizes", None)
    if hidden_sizes:
        return int(hidden_sizes[-1])
    num_channels = getattr(backbone_config, "num_channels", None)
    if num_channels is not None:
        return int(num_channels)
    raise ValueError("Could not infer hidden size from backbone config.")


@dataclass(slots=True)
class StudentModelConfig:
    backbone_model_name: str = "google/vit-base-patch16-224-in21k"
    hidden_dropout: float = 0.1
    use_lora: bool = True
    lora_r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.1
    medium_labels: tuple[str, ...] = ALL_MEDIA
    score_fields: tuple[str, ...] = SCORE_FIELDS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StudentModelConfig":
        payload = dict(payload)
        payload["medium_labels"] = tuple(payload.get("medium_labels", ALL_MEDIA))
        payload["score_fields"] = tuple(payload.get("score_fields", SCORE_FIELDS))
        return cls(**payload)


class GraffitiStudentModel(nn.Module):
    def __init__(
        self,
        config: StudentModelConfig,
        *,
        load_pretrained_backbone: bool,
    ) -> None:
        super().__init__()
        self.student_config = config
        if load_pretrained_backbone:
            backbone = AutoModel.from_pretrained(config.backbone_model_name)
        else:
            backbone_config = AutoConfig.from_pretrained(config.backbone_model_name)
            backbone = AutoModel.from_config(backbone_config)

        if config.use_lora:
            lora_config = LoraConfig(
                r=config.lora_r,
                lora_alpha=config.lora_alpha,
                lora_dropout=config.lora_dropout,
                target_modules=["query", "value"],
                task_type=TaskType.FEATURE_EXTRACTION,
            )
            backbone = get_peft_model(backbone, lora_config)

        self.backbone = backbone
        hidden_size = infer_hidden_size(backbone.config)
        self.dropout = nn.Dropout(config.hidden_dropout)
        self.usable_head = nn.Linear(hidden_size, 1)
        self.medium_head = nn.Linear(hidden_size, len(config.medium_labels))
        self.color_applicable_head = nn.Linear(hidden_size, 1)
        self.overall_head = nn.Linear(hidden_size, 1)
        self.score_heads = nn.ModuleDict({field: nn.Linear(hidden_size, 1) for field in config.score_fields})
    def forward(self, pixel_values):
        backbone_kwargs = {"pixel_values": pixel_values, "interpolate_pos_encoding": True}
        try:
            outputs = self.backbone(**backbone_kwargs)
        except TypeError as exc:
            if "interpolate_pos_encoding" not in str(exc):
                raise
            outputs = self.backbone(pixel_values=pixel_values)
        if getattr(outputs, "pooler_output", None) is not None:
            pooled = self.dropout(outputs.pooler_output)
        else:
            last_hidden_state = outputs.last_hidden_state
            if last_hidden_state.ndim == 3:
                pooled = self.dropout(last_hidden_state[:, 0])
            elif last_hidden_state.ndim == 4:
                pooled = self.dropout(last_hidden_state.mean(dim=(-1, -2)))
            else:
                raise ValueError(f"Unsupported last_hidden_state shape: {tuple(last_hidden_state.shape)}")
        result = {
            "usable_logits": self.usable_head(pooled).squeeze(-1),
            "medium_logits": self.medium_head(pooled),
            "color_applicable_logits": self.color_applicable_head(pooled).squeeze(-1),
            "overall_score": self.overall_head(pooled).squeeze(-1),
        }
        for field, head in self.score_heads.items():
            result[field] = head(pooled).squeeze(-1)
        return result

    @classmethod
    def from_saved_config(cls, path: Path) -> "GraffitiStudentModel":
        config = StudentModelConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))
        return cls(config, load_pretrained_backbone=False)
