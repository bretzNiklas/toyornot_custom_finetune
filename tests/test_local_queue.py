from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from deploy.local_queue import JOB_STATUS_COMPLETED, JOB_STATUS_PROCESSING, JOB_STATUS_QUEUED, LocalJobQueue, QueueConfig


def build_config(root: Path) -> QueueConfig:
    return QueueConfig(
        runtime_root=root,
        jobs_db_path=root / "jobs.sqlite3",
        spool_dir=root / "spool",
        worker_concurrency=1,
        job_lease_seconds=1,
        max_retries=2,
        max_estimated_wait_seconds=90,
        job_retention_hours=1,
        worker_heartbeat_timeout_seconds=45,
        worker_heartbeat_interval_seconds=5,
        worker_idle_poll_seconds=0.1,
        default_processing_seconds=5.0,
        processing_average_window=20,
        orphan_payload_grace_seconds=1,
    )


class LocalQueueTests(unittest.TestCase):
    def test_enqueue_and_queue_position_are_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            queue = LocalJobQueue(build_config(Path(temp_dir)))
            queue.initialize()
            first_payload = queue.config.spool_dir / "first.img"
            second_payload = queue.config.spool_dir / "second.img"
            first_payload.write_bytes(b"one")
            second_payload.write_bytes(b"two")

            first = queue.enqueue_job(
                job_id=str(uuid4()),
                request_id=str(uuid4()),
                filename="first.png",
                include_debug=False,
                payload_path=first_payload,
                payload_size_bytes=3,
            )
            second = queue.enqueue_job(
                job_id=str(uuid4()),
                request_id=str(uuid4()),
                filename="second.png",
                include_debug=False,
                payload_path=second_payload,
                payload_size_bytes=3,
            )

            second_job = queue.get_job(second.job_id)

            self.assertTrue(first.accepted)
            self.assertEqual(second.queue_position, 2)
            assert second_job is not None
            self.assertEqual(queue.get_job_queue_position(second_job), 2)
            self.assertEqual(second_job.status, JOB_STATUS_QUEUED)

    def test_expired_processing_job_can_be_reclaimed(self) -> None:
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

            claimed_once = queue.claim_next_job("worker-1")
            assert claimed_once is not None
            with queue._connection() as connection:
                connection.execute(
                    """
                    update jobs
                    set lease_expires_at = ?
                    where job_id = ?
                    """,
                    (time.time() - 5, job_id),
                )

            reclaimed = queue.claim_next_job("worker-2")

            assert reclaimed is not None
            self.assertEqual(claimed_once.status, JOB_STATUS_PROCESSING)
            self.assertEqual(claimed_once.attempt_count, 1)
            self.assertEqual(reclaimed.job_id, job_id)
            self.assertEqual(reclaimed.attempt_count, 2)

    def test_cleanup_removes_old_terminal_jobs_and_orphan_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            queue = LocalJobQueue(build_config(Path(temp_dir)))
            queue.initialize()
            payload_path = queue.config.spool_dir / "old.img"
            payload_path.write_bytes(b"payload")
            job_id = str(uuid4())
            queue.enqueue_job(
                job_id=job_id,
                request_id=str(uuid4()),
                filename="old.png",
                include_debug=False,
                payload_path=payload_path,
                payload_size_bytes=7,
            )
            queue.complete_job(
                job_id=job_id,
                result_payload={"request_id": "req", "model_version": "student-v2-dinov2"},
                processing_duration_ms=500.0,
            )
            old_time = time.time() - 4000
            with queue._connection() as connection:
                connection.execute(
                    """
                    update jobs
                    set completed_at = ?
                    where job_id = ?
                    """,
                    (old_time, job_id),
                )
            os_utime = old_time - 10
            payload_path.touch()
            payload_path.chmod(0o666)
            os.utime(payload_path, (os_utime, os_utime))
            queue.cleanup()
            job = queue.get_job(job_id)

            self.assertIsNone(job)
            self.assertFalse(payload_path.exists())


if __name__ == "__main__":
    unittest.main()
