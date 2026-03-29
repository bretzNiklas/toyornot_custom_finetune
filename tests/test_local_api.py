from __future__ import annotations

import base64
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
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


class FakePredictionService:
    instances: list["FakePredictionService"] = []

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        FakePredictionService.instances.append(self)

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
def local_api_client(
    runtime_root: Path | None = None,
    *,
    extra_env: dict[str, str] | None = None,
):
    owns_runtime = runtime_root is None
    temp_dir = tempfile.TemporaryDirectory() if owns_runtime else None
    active_root = runtime_root or Path(temp_dir.name)
    env = {
        "AUTH_TOKEN": "test-token",
        "RUNTIME_ROOT": str(active_root),
        "JOBS_DB_PATH": str(active_root / "jobs.sqlite3"),
        "JOB_SPOOL_DIR": str(active_root / "spool"),
        "WORKER_CONCURRENCY": "1",
        "DEFAULT_PROCESSING_SECONDS": "5.0",
        "MAX_ESTIMATED_WAIT_SECONDS": "90",
    }
    if extra_env:
        env.update(extra_env)
    try:
        with patch.dict(os.environ, env, clear=False):
            with patch.object(local_api, "PredictionService", FakePredictionService):
                FakePredictionService.instances = []
                local_api.queue = None
                local_api.prediction_service = None
                with TestClient(local_api.app) as client:
                    yield client
    finally:
        local_api.queue = None
        local_api.prediction_service = None
        if temp_dir is not None:
            temp_dir.cleanup()


class LocalApiTests(unittest.TestCase):
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-token"}

    def test_missing_bearer_token_is_rejected(self) -> None:
        with local_api_client() as client:
            response = client.post("/predict", json={"image_b64": encode_png()})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Not authenticated")

    def test_invalid_bearer_token_is_rejected(self) -> None:
        with local_api_client() as client:
            response = client.post(
                "/predict",
                headers={"Authorization": "Bearer wrong-token"},
                json={"image_b64": encode_png()},
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Invalid bearer token.")

    def test_invalid_base64_returns_structured_error(self) -> None:
        with local_api_client() as client:
            response = client.post(
                "/predict",
                headers=self.auth_headers(),
                json={"image_b64": "%%%"},
            )

        body = response.json()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(body["error"], "invalid_base64")
        UUID(body["request_id"])

    def test_predict_returns_rating_payload(self) -> None:
        with local_api_client() as client:
            response = client.post(
                "/predict",
                headers=self.auth_headers(),
                json={"image_b64": encode_png(), "filename": "example.png"},
            )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["filename"], "example.png")
        self.assertEqual(body["image_usable"], True)
        self.assertEqual(body["medium"], "wall_piece")
        self.assertEqual(body["overall_score"], 7)
        self.assertEqual(body["model_version"], local_api.MODEL_VERSION)
        UUID(body["request_id"])
        self.assertEqual(FakePredictionService.instances[0].calls[0]["size"], (64, 64))

    def test_include_debug_is_passed_through_to_predictor(self) -> None:
        with local_api_client() as client:
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
        self.assertEqual(FakePredictionService.instances[0].calls[0]["include_debug"], True)

    def test_create_prediction_job_returns_accepted(self) -> None:
        with local_api_client() as client:
            response = client.post(
                "/predictions",
                headers=self.auth_headers(),
                json={"image_b64": encode_png(), "filename": "queued.png"},
            )

        body = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["queue_position"], 1)
        self.assertTrue(body["poll_url"].endswith(f"/predictions/{body['job_id']}"))
        UUID(body["job_id"])
        UUID(body["request_id"])

    def test_prediction_status_reports_queue_position(self) -> None:
        with local_api_client() as client:
            created = client.post(
                "/predictions",
                headers=self.auth_headers(),
                json={"image_b64": encode_png(), "filename": "queued.png"},
            ).json()

            response = client.get(
                f"/predictions/{created['job_id']}",
                headers=self.auth_headers(),
            )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["queue_position"], 1)
        self.assertGreaterEqual(body["estimated_wait_seconds"], 1)

    def test_prediction_status_long_poll_returns_terminal_result(self) -> None:
        with local_api_client() as client:
            created = client.post(
                "/predictions",
                headers=self.auth_headers(),
                json={"image_b64": encode_png(), "filename": "queued.png"},
            ).json()

            def complete_job() -> None:
                time.sleep(0.2)
                job = local_api.get_queue().get_job(created["job_id"])
                assert job is not None
                local_api.get_queue().complete_job(
                    job_id=job.job_id,
                    result_payload={
                        "filename": "queued.png",
                        "image_usable": True,
                        "medium": "wall_piece",
                        "overall_score": 8,
                        "legibility": 7,
                        "letter_structure": 8,
                        "line_quality": 8,
                        "composition": 7,
                        "color_harmony": 7,
                        "originality": 8,
                        "request_id": job.request_id,
                        "model_version": local_api.MODEL_VERSION,
                    },
                    processing_duration_ms=850.0,
                )

            worker = threading.Thread(target=complete_job)
            worker.start()
            response = client.get(
                f"/predictions/{created['job_id']}?wait_ms=1000",
                headers=self.auth_headers(),
            )
            worker.join()

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["result"]["overall_score"], 8)

    def test_prediction_submission_returns_429_when_estimated_wait_is_too_high(self) -> None:
        with local_api_client(extra_env={"MAX_ESTIMATED_WAIT_SECONDS": "1"}) as client:
            response = client.post(
                "/predictions",
                headers=self.auth_headers(),
                json={"image_b64": encode_png(), "filename": "busy.png"},
            )

        body = response.json()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(body["error"], "queue_overloaded")
        self.assertIn("Retry-After", response.headers)

    def test_jobs_survive_api_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir)
            with local_api_client(runtime_root) as client:
                created = client.post(
                    "/predictions",
                    headers=self.auth_headers(),
                    json={"image_b64": encode_png(), "filename": "persisted.png"},
                ).json()

            with local_api_client(runtime_root) as client:
                response = client.get(
                    f"/predictions/{created['job_id']}",
                    headers=self.auth_headers(),
                )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["job_id"], created["job_id"])
        self.assertEqual(body["status"], "queued")

    def test_health_requires_fresh_worker_heartbeat(self) -> None:
        with local_api_client() as client:
            degraded = client.get("/health", headers=self.auth_headers())
            local_api.get_queue().heartbeat_worker("worker-1", current_job_id=None, status="idle")
            healthy = client.get("/health", headers=self.auth_headers())

        self.assertEqual(degraded.status_code, 503)
        self.assertEqual(healthy.status_code, 200)
        self.assertEqual(healthy.json()["worker_heartbeat_fresh"], True)


if __name__ == "__main__":
    unittest.main()
