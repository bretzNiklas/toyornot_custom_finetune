from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import torch
from PIL import Image

from .checkpoint import load_student_bundle
from .constants import CORE_MEDIA, SCORE_FIELDS, clamp_score
from .data import decode_image_b64


def sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


class StudentPredictor:
    def __init__(self, model_dir: Path, *, device: str | None = None) -> None:
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = torch.device(device)
        self.model, self.processor, self.thresholds = load_student_bundle(Path(model_dir), self.device)
        self.medium_labels = list(self.model.student_config.medium_labels)

    def _predict_raw(self, image: Image.Image) -> dict[str, float | str]:
        inputs = self.processor(images=image.convert("RGB"), return_tensors="pt")
        pixel_values = inputs["pixel_values"].to(self.device)
        with torch.inference_mode():
            outputs = self.model(pixel_values=pixel_values)
        usable_probability = sigmoid(outputs["usable_logits"][0].item())
        medium_index = int(outputs["medium_logits"][0].argmax().item())
        medium_prediction = self.medium_labels[medium_index]
        color_probability = sigmoid(outputs["color_applicable_logits"][0].item())
        raw_scores: dict[str, float | str] = {
            "usable_probability": usable_probability,
            "medium_prediction": medium_prediction,
            "color_applicable_probability": color_probability,
            "overall_score": outputs["overall_score"][0].item(),
        }
        for field in SCORE_FIELDS:
            raw_scores[field] = outputs[field][0].item()
        return raw_scores

    def predict_image(
        self,
        image: Image.Image,
        *,
        filename: str | None = None,
        include_debug: bool = False,
    ) -> dict[str, Any]:
        raw = self._predict_raw(image)
        usable_threshold = float(self.thresholds.get("usable", 0.5))
        color_threshold = float(self.thresholds.get("color_applicable", 0.5))
        usable = raw["usable_probability"] >= usable_threshold
        medium = str(raw["medium_prediction"])

        payload: dict[str, Any] = {
            "filename": filename,
            "image_usable": bool(usable),
            "medium": medium,
            "overall_score": None,
        }
        for field in SCORE_FIELDS:
            payload[field] = None

        if usable and medium in CORE_MEDIA:
            payload["overall_score"] = clamp_score(float(raw["overall_score"]))
            color_applicable = raw["color_applicable_probability"] >= color_threshold
            for field in SCORE_FIELDS:
                if field == "color_harmony" and not color_applicable:
                    payload[field] = None
                else:
                    payload[field] = clamp_score(float(raw[field]))

        if include_debug:
            payload["debug"] = {
                "usable_probability": raw["usable_probability"],
                "usable_threshold": usable_threshold,
                "color_applicable_probability": raw["color_applicable_probability"],
                "color_threshold": color_threshold,
            }
        return payload

    def predict_base64(
        self,
        image_b64: str,
        *,
        filename: str | None = None,
        include_debug: bool = False,
    ) -> dict[str, Any]:
        image = decode_image_b64(image_b64)
        return self.predict_image(image, filename=filename, include_debug=include_debug)
