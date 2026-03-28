from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from io import BytesIO
from pathlib import Path

import modal
from fastapi import Depends, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel


APP_NAME = "graffiti-student-v1"
MODEL_DIR = Path("runs/student_v1/stage_b/best_bundle")
REMOTE_MODEL_DIR = "/root/model_bundle"
MODEL_VERSION = os.environ.get("MODEL_VERSION", "student-v1")
AUTH_SECRET_NAME = "graffiti-student-web-auth"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MIN_DIMENSION = 32
MAX_DIMENSION = 8192

logger = logging.getLogger("graffiti_student_modal")
auth_scheme = HTTPBearer(auto_error=False)


image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "torch>=2.4.0",
        "torchvision>=0.19.0",
        "transformers>=4.55.0",
        "peft>=0.13.0",
        "accelerate>=0.34.0",
        "safetensors>=0.4.5",
        "Pillow>=10.4.0",
        "pydantic>=2.8.0",
        "fastapi[standard]>=0.115.0",
    )
    .add_local_python_source("student")
    .add_local_dir(MODEL_DIR, remote_path=REMOTE_MODEL_DIR)
)

app = modal.App(APP_NAME, image=image)


class PredictionRequest(BaseModel):
    image_b64: str
    filename: str | None = None
    include_debug: bool = False


def authorize(token: HTTPAuthorizationCredentials | None = Depends(auth_scheme)) -> str:
    expected = os.environ.get("AUTH_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server auth is not configured.",
        )
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authenticated",
        )
    if token.credentials != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token.credentials


def decode_and_validate_image(image_b64: str) -> Image.Image:
    try:
        raw = base64.b64decode(image_b64, validate=True)
    except Exception as exc:
        raise ValueError("invalid_base64") from exc

    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("image_too_large")

    try:
        image = Image.open(BytesIO(raw))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("invalid_image") from exc

    width, height = image.size
    if min(width, height) < MIN_DIMENSION:
        raise ValueError("image_too_small")
    if max(width, height) > MAX_DIMENSION:
        raise ValueError("image_too_large_dimensions")

    return image.convert("RGB")


def error_response(code: str, message: str, status_code: int, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": code,
            "message": message,
            "request_id": request_id,
            "model_version": MODEL_VERSION,
        },
    )


@app.cls(
    gpu="T4",
    scaledown_window=300,
    timeout=300,
    secrets=[modal.Secret.from_name(AUTH_SECRET_NAME)],
)
class GraffitiStudentService:
    @modal.enter()
    def load(self) -> None:
        from student.predictor import StudentPredictor

        self.predictor = StudentPredictor(Path(REMOTE_MODEL_DIR))

    @modal.fastapi_endpoint(method="GET", docs=False)
    def health(self, _: str = Depends(authorize)) -> dict:
        return {
            "status": "ok",
            "model_version": MODEL_VERSION,
            "app": APP_NAME,
        }

    @modal.fastapi_endpoint(method="POST", docs=False)
    def predict(
        self,
        payload: PredictionRequest,
        _: str = Depends(authorize),
    ):
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        try:
            image = decode_and_validate_image(payload.image_b64)
            result = self.predictor.predict_image(
                image,
                filename=payload.filename,
                include_debug=payload.include_debug,
            )
            result["request_id"] = request_id
            result["model_version"] = MODEL_VERSION
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.info(
                json.dumps(
                    {
                        "event": "prediction",
                        "request_id": request_id,
                        "filename": payload.filename,
                        "latency_ms": latency_ms,
                        "image_usable": result.get("image_usable"),
                        "medium": result.get("medium"),
                        "overall_score": result.get("overall_score"),
                    }
                )
            )
            return result
        except ValueError as exc:
            code = str(exc)
            message_map = {
                "invalid_base64": "Could not decode the request body as base64.",
                "invalid_image": "The uploaded content is not a valid image.",
                "image_too_large": "The uploaded image exceeds the size limit.",
                "image_too_small": "The uploaded image is too small to score reliably.",
                "image_too_large_dimensions": "The uploaded image dimensions exceed the allowed limit.",
            }
            return error_response(code, message_map.get(code, "Invalid request."), 400, request_id)
        except HTTPException:
            raise
        except Exception:
            logger.exception("Unhandled prediction error", extra={"request_id": request_id})
            return error_response(
                "internal_error",
                "Unexpected server error.",
                500,
                request_id,
            )
