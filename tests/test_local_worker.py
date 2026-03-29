from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from deploy.local_queue import JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_QUEUED, LocalJobQueue, QueueConfig
from deploy.local_worker import process_claimed_job


def build_config(root: Path) -> QueueConfig:
    return QueueConfig(
        runtime_root=root,
        jobs_db_path=root / "jobs.sqlite3",
        spool_dir=root / "spool",
        worker_concurrency=1,
        job_lease_seconds=30,
        max_retries=2,
        max_estimated_wait_seconds=90,
        job_retention_hours=24,
        worker_heartbeat_timeout_seconds=45,
        worker_heartbeat_interval_seconds=5,
        worker_idle_poll_seconds=0.1,
        default_processing_seconds=5.0,
        processing_average_window=20,
        orphan_payload_grace_seconds=300,
    )


class FakePredictor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def predict_bytes(self, raw: bytes, *, filename: str | None = None, include_debug: bool = False):
        self.calls.append({"raw": raw, "filename": filename, "include_debug": include_debug})
        return {
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


class FailingPredictor:
    def predict_bytes(self, raw: bytes, *, filename: str | None = None, include_debug: bool = False):
        raise RuntimeError("temporary failure")


class LocalWorkerTests(unittest.TestCase):
    def test_process_claimed_job_marks_job_completed_and_removes_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            queue = LocalJobQueue(build_config(Path(temp_dir)))
            queue.initialize()
            payload_path = queue.config.spool_dir / "piece.img"
            payload_path.write_bytes(b"payload")
            job_id = str(uuid4())
            request_id = str(uuid4())
            queue.enqueue_job(
                job_id=job_id,
                request_id=request_id,
                filename="piece.png",
                include_debug=True,
                payload_path=payload_path,
                payload_size_bytes=7,
            )
            job = queue.claim_next_job("worker-1")
            assert job is not None

            predictor = FakePredictor()
            process_claimed_job(queue, predictor, job)
            stored = queue.get_job(job_id)

            assert stored is not None
            self.assertEqual(stored.status, JOB_STATUS_COMPLETED)
            self.assertFalse(payload_path.exists())
            self.assertEqual(stored.result_json["request_id"], request_id)
            self.assertEqual(predictor.calls[0]["filename"], "piece.png")
            self.assertEqual(predictor.calls[0]["include_debug"], True)

    def test_process_claimed_job_requeues_transient_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            queue = LocalJobQueue(build_config(Path(temp_dir)))
            queue.initialize()
            payload_path = queue.config.spool_dir / "piece.img"
            payload_path.write_bytes(b"payload")
            job_id = str(uuid4())
            queue.enqueue_job(
                job_id=job_id,
                request_id=str(uuid4()),
                filename="piece.png",
                include_debug=False,
                payload_path=payload_path,
                payload_size_bytes=7,
            )
            job = queue.claim_next_job("worker-1")
            assert job is not None

            process_claimed_job(queue, FailingPredictor(), job)
            stored = queue.get_job(job_id)

            assert stored is not None
            self.assertEqual(stored.status, JOB_STATUS_QUEUED)
            self.assertTrue(payload_path.exists())

    def test_missing_payload_fails_job_permanently(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            queue = LocalJobQueue(build_config(Path(temp_dir)))
            queue.initialize()
            payload_path = queue.config.spool_dir / "piece.img"
            payload_path.write_bytes(b"payload")
            job_id = str(uuid4())
            queue.enqueue_job(
                job_id=job_id,
                request_id=str(uuid4()),
                filename="piece.png",
                include_debug=False,
                payload_path=payload_path,
                payload_size_bytes=7,
            )
            payload_path.unlink()
            job = queue.claim_next_job("worker-1")
            assert job is not None

            process_claimed_job(queue, FakePredictor(), job)
            stored = queue.get_job(job_id)

            assert stored is not None
            self.assertEqual(stored.status, JOB_STATUS_FAILED)
            self.assertEqual(stored.error_code, "payload_missing")


if __name__ == "__main__":
    unittest.main()
