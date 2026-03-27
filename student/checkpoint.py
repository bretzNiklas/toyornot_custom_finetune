from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import torch
from safetensors.torch import load_file, save_file
from transformers import AutoImageProcessor

from .model import GraffitiStudentModel


def save_student_bundle(
    *,
    model: GraffitiStudentModel,
    processor: Any,
    output_dir: Path,
    training_config: dict[str, Any],
    metrics: dict[str, Any],
    thresholds: dict[str, float],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    processor.save_pretrained(output_dir)
    (output_dir / "student_config.json").write_text(
        json.dumps(model.student_config.to_dict(), indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "training_config.json").write_text(
        json.dumps(training_config, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "thresholds.json").write_text(
        json.dumps(thresholds, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    state_dict = {key: value.detach().cpu() for key, value in model.state_dict().items()}
    save_file(state_dict, str(output_dir / "model.safetensors"))


def load_student_bundle(model_dir: Path, device: torch.device):
    model = GraffitiStudentModel.from_saved_config(model_dir / "student_config.json")
    state_dict = load_file(str(model_dir / "model.safetensors"))
    model.load_state_dict(state_dict)
    model.to(device)
    model.eval()
    processor = AutoImageProcessor.from_pretrained(model_dir)
    thresholds_path = model_dir / "thresholds.json"
    thresholds = json.loads(thresholds_path.read_text(encoding="utf-8")) if thresholds_path.exists() else {}
    return model, processor, thresholds
