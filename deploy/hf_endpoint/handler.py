from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from student.predictor import StudentPredictor


class EndpointHandler:
    def __init__(self, path: str = "") -> None:
        model_dir = Path(path or os.getcwd())
        self.predictor = StudentPredictor(model_dir)

    def __call__(self, data: dict[str, Any]) -> dict[str, Any]:
        inputs = data.get("inputs") or data
        image_b64 = inputs.get("image_b64")
        if not image_b64:
            raise ValueError("Request must include image_b64.")
        filename = inputs.get("filename")
        include_debug = bool(inputs.get("include_debug", False))
        return self.predictor.predict_base64(
            image_b64,
            filename=filename,
            include_debug=include_debug,
        )
