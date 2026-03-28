from __future__ import annotations

import base64
import os
import sys
import types
import unittest
from contextlib import contextmanager
from io import BytesIO
from uuid import UUID
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

fake_student_predictor = types.ModuleType("student.predictor")
fake_student_predictor.StudentPredictor = object
sys.modules.setdefault("student.predictor", fake_student_predictor)

import deploy.local_api as local_api


def encode_bytes(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def encode_png(width: int = 64, height: int = 64) -> str:
    buffer = BytesIO()
    Image.new("RGB", (width, height), color=(32, 96, 160)).save(buffer, format="PNG")
    return encode_bytes(buffer.getvalue())


class FakePredictor:
    def __init__(self, model_dir) -> None:
        self.model_dir = model_dir
        self.calls: list[dict[str, object]] = []

    def predict_image(self, image: Image.Image, *, filename: str | None = None, include_debug: bool = False):
        self.calls.append(
            {
                "size": image.size,
                "filename": filename,
                "include_debug": include_debug,
            }
        )
        payload = {
            "filename": filename,
            "image_usable": True,
            "medium": "wall_piece",
            "overall_score": 7,
            "legibility": 6,
            "letter_structure": 7,
            "line_quality": 8,
            "composition": 7,
            "color_harmony": 6,
            "originality": 8,
        }
        if include_debug:
            payload["debug"] = {
                "usable_probability": 0.98,
                "usable_threshold": 0.5,
                "color_applicable_probability": 0.72,
                "color_threshold": 0.45,
            }
        return payload


@contextmanager
def local_api_client():
    with patch.dict(os.environ, {"AUTH_TOKEN": "test-token"}, clear=False):
        with patch.object(local_api, "StudentPredictor", FakePredictor):
            local_api.predictor = None
            with TestClient(local_api.app) as client:
                yield client, local_api.predictor
            local_api.predictor = None


class LocalApiTests(unittest.TestCase):
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-token"}

    def test_missing_bearer_token_is_rejected(self) -> None:
        with local_api_client() as (client, _):
            response = client.post("/predict", json={"image_b64": encode_png()})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Not authenticated")

    def test_invalid_bearer_token_is_rejected(self) -> None:
        with local_api_client() as (client, _):
            response = client.post(
                "/predict",
                headers={"Authorization": "Bearer wrong-token"},
                json={"image_b64": encode_png()},
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Invalid bearer token.")

    def test_invalid_base64_returns_structured_error(self) -> None:
        with local_api_client() as (client, _):
            response = client.post(
                "/predict",
                headers=self.auth_headers(),
                json={"image_b64": "%%%"},
            )

        body = response.json()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(body["error"], "invalid_base64")
        self.assertEqual(body["model_version"], local_api.MODEL_VERSION)
        UUID(body["request_id"])

    def test_invalid_image_returns_structured_error(self) -> None:
        with local_api_client() as (client, _):
            response = client.post(
                "/predict",
                headers=self.auth_headers(),
                json={"image_b64": encode_bytes(b"not-an-image")},
            )

        body = response.json()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(body["error"], "invalid_image")
        self.assertEqual(body["message"], "The uploaded content is not a valid image.")

    def test_too_small_image_returns_structured_error(self) -> None:
        with local_api_client() as (client, _):
            response = client.post(
                "/predict",
                headers=self.auth_headers(),
                json={"image_b64": encode_png(width=16, height=16)},
            )

        body = response.json()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(body["error"], "image_too_small")
        self.assertEqual(body["message"], "The uploaded image is too small to score reliably.")

    def test_too_large_payload_returns_structured_error(self) -> None:
        with local_api_client() as (client, _):
            with patch.object(local_api, "MAX_IMAGE_BYTES", 16):
                response = client.post(
                    "/predict",
                    headers=self.auth_headers(),
                    json={"image_b64": encode_bytes(b"x" * 17)},
                )

        body = response.json()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(body["error"], "image_too_large")
        self.assertEqual(body["message"], "The uploaded image exceeds the size limit.")

    def test_predict_returns_rating_payload(self) -> None:
        with local_api_client() as (client, predictor):
            response = client.post(
                "/predict",
                headers=self.auth_headers(),
                json={
                    "image_b64": encode_png(),
                    "filename": "example.png",
                },
            )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["filename"], "example.png")
        self.assertEqual(body["image_usable"], True)
        self.assertEqual(body["medium"], "wall_piece")
        self.assertEqual(body["overall_score"], 7)
        self.assertEqual(body["letter_structure"], 7)
        self.assertEqual(body["line_quality"], 8)
        self.assertEqual(body["model_version"], local_api.MODEL_VERSION)
        UUID(body["request_id"])
        self.assertEqual(predictor.calls[0]["size"], (64, 64))
        self.assertEqual(predictor.calls[0]["filename"], "example.png")

    def test_include_debug_is_passed_through_to_predictor(self) -> None:
        with local_api_client() as (client, predictor):
            response = client.post(
                "/predict",
                headers=self.auth_headers(),
                json={
                    "image_b64": encode_png(),
                    "filename": "debug.png",
                    "include_debug": True,
                },
            )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertIn("debug", body)
        self.assertEqual(body["debug"]["usable_threshold"], 0.5)
        self.assertEqual(predictor.calls[0]["filename"], "debug.png")
        self.assertEqual(predictor.calls[0]["include_debug"], True)


if __name__ == "__main__":
    unittest.main()
