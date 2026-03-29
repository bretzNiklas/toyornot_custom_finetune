from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from student.predictor import StudentPredictor


MODEL_DIR = Path(os.environ.get("MODEL_DIR", "models/dinov2_base_224"))
MODEL_VERSION = os.environ.get("MODEL_VERSION", "student-v2-dinov2")
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MIN_DIMENSION = 32
MAX_DIMENSION = 8192

ERROR_MESSAGES = {
    "invalid_base64": "Could not decode the request body as base64.",
    "invalid_image": "The uploaded content is not a valid image.",
    "image_too_large": "The uploaded image exceeds the size limit.",
    "image_too_small": "The uploaded image is too small to score reliably.",
    "image_too_large_dimensions": "The uploaded image dimensions exceed the allowed limit.",
    "payload_missing": "The queued image payload is missing from local storage.",
}


class PredictionValidationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    @property
    def message(self) -> str:
        return ERROR_MESSAGES.get(self.code, "Invalid request.")


@dataclass(frozen=True)
class ValidatedImagePayload:
    raw_bytes: bytes
    image: Image.Image


def decode_and_validate_image(image_b64: str) -> ValidatedImagePayload:
    try:
        raw = base64.b64decode(image_b64, validate=True)
    except Exception as exc:
        raise PredictionValidationError("invalid_base64") from exc
    return validate_image_bytes(raw)


def validate_image_bytes(raw: bytes) -> ValidatedImagePayload:
    if len(raw) > MAX_IMAGE_BYTES:
        raise PredictionValidationError("image_too_large")

    try:
        image = Image.open(BytesIO(raw))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise PredictionValidationError("invalid_image") from exc

    width, height = image.size
    if min(width, height) < MIN_DIMENSION:
        raise PredictionValidationError("image_too_small")
    if max(width, height) > MAX_DIMENSION:
        raise PredictionValidationError("image_too_large_dimensions")

    return ValidatedImagePayload(raw_bytes=raw, image=image.convert("RGB"))


class PredictionService:
    def __init__(
        self,
        *,
        model_dir: Path = MODEL_DIR,
        model_version: str = MODEL_VERSION,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.model_version = model_version
        self._predictor: StudentPredictor | None = None

    def predict_base64(
        self,
        image_b64: str,
        *,
        filename: str | None = None,
        include_debug: bool = False,
    ) -> dict[str, Any]:
        payload = decode_and_validate_image(image_b64)
        return self.predict_image(payload.image, filename=filename, include_debug=include_debug)

    def predict_bytes(
        self,
        raw: bytes,
        *,
        filename: str | None = None,
        include_debug: bool = False,
    ) -> dict[str, Any]:
        payload = validate_image_bytes(raw)
        return self.predict_image(payload.image, filename=filename, include_debug=include_debug)

    def predict_image(
        self,
        image: Image.Image,
        *,
        filename: str | None = None,
        include_debug: bool = False,
    ) -> dict[str, Any]:
        predictor = self._get_predictor()
        return predictor.predict_image(image, filename=filename, include_debug=include_debug)

    def _get_predictor(self) -> StudentPredictor:
        if self._predictor is None:
            self._predictor = StudentPredictor(self.model_dir)
        return self._predictor
