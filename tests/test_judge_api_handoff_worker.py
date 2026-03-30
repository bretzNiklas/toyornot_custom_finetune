from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from deploy.judge_api_handoff_runtime import (
    ArchivedJudgeImage,
    JUDGE_JOB_STATUS_CLAIMED,
    JUDGE_JOB_STATUS_COMPLETED,
    JUDGE_JOB_STATUS_FAILED,
    JUDGE_JOB_STATUS_PENDING,
    JUDGE_JOB_STATUS_PROCESSING,
    JudgeApiHandoffConfig,
    JudgeApiJob,
    JudgeApiResultRecord,
    PiecerateStatusResponse,
    PiecerateSubmission,
    RetryableWorkerError,
    SupabaseJudgeApiRuntime,
)
from deploy.judge_api_handoff_worker import process_handoff_job, run_worker_iteration
from deploy.judge_api_handoff_worker import run_worker_service


def build_config() -> JudgeApiHandoffConfig:
    return JudgeApiHandoffConfig(
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-key",
        judge_api_token="judge-token",
        judge_api_base_url="https://api.piecerate.me",
        judge_api_timeout_ms=30_000,
        jobs_table="judge_api_jobs",
        results_table="judge_api_results",
        input_bucket="judge-api-inputs",
        judged_image_archive_dir=Path("/srv/graffiti-student/runtime/judged-images"),
        worker_id="worker-1",
        lock_timeout_seconds=600,
        lock_refresh_seconds=120,
        poll_wait_ms=8_000,
        idle_sleep_seconds=0.01,
        safety_sweep_seconds=600,
        max_attempts=5,
        backoff_schedule_seconds=(30, 120, 600, 600),
    )


def build_job(
    *,
    status: str = JUDGE_JOB_STATUS_CLAIMED,
    worker_attempt_count: int = 1,
    created_at: str | None = "2026-03-29T18:55:00+00:00",
    started_at: str | None = "2026-03-29T19:00:00+00:00",
    locked_at: str | None = None,
    locked_by: str | None = None,
    piecerate_job_id: str | None = None,
    piecerate_request_id: str | None = None,
) -> JudgeApiJob:
    if locked_at is None and status in {JUDGE_JOB_STATUS_CLAIMED, JUDGE_JOB_STATUS_PROCESSING}:
        locked_at = "2026-03-29T19:00:00+00:00"
    if locked_by is None and locked_at is not None:
        locked_by = "worker-1"
    return JudgeApiJob(
        request_id="request-1",
        status=status,
        created_at=created_at,
        started_at=started_at,
        input_storage_bucket="judge-api-inputs",
        input_storage_path="inputs/request-1.png",
        filename="piece.png",
        image_mime_type="image/png",
        image_size_bytes=1234,
        judge_image_hash_sha256="judge-hash",
        base_image_hash_sha256="base-hash",
        llm_judgement_engine_id="judge-api-v2",
        llm_model="student-v2-dinov2",
        worker_attempt_count=worker_attempt_count,
        next_attempt_at=None,
        locked_at=locked_at,
        locked_by=locked_by,
        piecerate_job_id=piecerate_job_id,
        piecerate_request_id=piecerate_request_id,
        last_error=None,
        completed_at=None,
    )


def build_job_row(job: JudgeApiJob) -> dict[str, object]:
    return {
        "request_id": job.request_id,
        "status": job.status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "input_storage_bucket": job.input_storage_bucket,
        "input_storage_path": job.input_storage_path,
        "filename": job.filename,
        "image_mime_type": job.image_mime_type,
        "image_size_bytes": job.image_size_bytes,
        "judge_image_hash_sha256": job.judge_image_hash_sha256,
        "base_image_hash_sha256": job.base_image_hash_sha256,
        "llm_judgement_engine_id": job.llm_judgement_engine_id,
        "llm_model": job.llm_model,
        "worker_attempt_count": job.worker_attempt_count,
        "next_attempt_at": job.next_attempt_at,
        "locked_at": job.locked_at,
        "locked_by": job.locked_by,
        "piecerate_job_id": job.piecerate_job_id,
        "piecerate_request_id": job.piecerate_request_id,
        "last_error": job.last_error,
        "completed_at": job.completed_at,
    }


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeRpcCall:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return FakeResponse(self.data)


class FakeSupabaseClaimClient:
    def __init__(self, rpc_data):
        self.rpc_data = rpc_data
        self.rpc_calls: list[tuple[str, dict[str, object]]] = []

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        return FakeRpcCall(self.rpc_data)


class FakeMutableJobsTable:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = dict(row)
        self._payload: dict[str, object] | None = None
        self._filters: list[tuple[str, object]] = []
        self.update_payloads: list[dict[str, object]] = []

    def update(self, payload):
        self._payload = dict(payload)
        self.update_payloads.append(dict(payload))
        self._filters = []
        return self

    def eq(self, key, value):
        self._filters.append((key, value))
        return self

    def execute(self):
        if self._payload is None:
            return FakeResponse([])
        for key, value in self._filters:
            if self.row.get(key) != value:
                return FakeResponse([])
        self.row.update(self._payload)
        return FakeResponse([dict(self.row)])


class FakeMutableJobsClient:
    def __init__(self, row: dict[str, object]) -> None:
        self.jobs = FakeMutableJobsTable(row)

    def table(self, name):
        if name != "judge_api_jobs":
            raise AssertionError(f"Unexpected table {name}")
        return self.jobs


class FakeRuntime:
    def __init__(
        self,
        config: JudgeApiHandoffConfig,
        *,
        claimed_jobs: list[JudgeApiJob] | None = None,
        existing_result: JudgeApiResultRecord | None = None,
        next_retry_values: list[datetime | None] | None = None,
    ) -> None:
        self.config = config
        self.claimed_jobs = list(claimed_jobs or [])
        self.existing_result = existing_result
        self.next_retry_values = list(next_retry_values or [])
        self.claim_calls = 0
        self.download_calls: list[JudgeApiJob] = []
        self.processing_updates: list[dict[str, object]] = []
        self.refresh_updates: list[dict[str, object]] = []
        self.upserted_results: list[dict[str, object]] = []
        self.completed_updates: list[dict[str, object]] = []
        self.failed_updates: list[dict[str, object]] = []
        self.requeued_updates: list[dict[str, object]] = []
        self.finalized_existing: list[tuple[JudgeApiJob, JudgeApiResultRecord]] = []
        self.deleted_inputs: list[JudgeApiJob] = []
        self.archived_inputs: list[tuple[JudgeApiJob, ArchivedJudgeImage]] = []
        self.ensured_archives: list[JudgeApiJob] = []

    def claim_next_job(self) -> JudgeApiJob | None:
        self.claim_calls += 1
        if not self.claimed_jobs:
            return None
        return self.claimed_jobs.pop(0)

    def get_result_by_request_id(self, request_id: str) -> JudgeApiResultRecord | None:
        return self.existing_result

    def download_input_bytes(self, job: JudgeApiJob) -> bytes:
        self.download_calls.append(job)
        return b"image-bytes"

    def archive_input_image(self, job: JudgeApiJob, raw_bytes: bytes) -> ArchivedJudgeImage:
        archived = ArchivedJudgeImage(
            local_path=self.config.judged_image_archive_dir / job.request_id / (job.filename or "request-1.img"),
            filename=job.filename or "request-1.img",
        )
        self.archived_inputs.append((job, archived))
        return archived

    def ensure_archived_input_image(self, job: JudgeApiJob) -> ArchivedJudgeImage:
        self.ensured_archives.append(job)
        for archived_job, archived in self.archived_inputs:
            if archived_job.request_id == job.request_id:
                return archived
        archived = ArchivedJudgeImage(
            local_path=self.config.judged_image_archive_dir / job.request_id / (job.filename or "request-1.img"),
            filename=job.filename or "request-1.img",
        )
        self.archived_inputs.append((job, archived))
        return archived

    def mark_job_processing(self, job: JudgeApiJob, *, piecerate_job_id: str, piecerate_request_id: str | None) -> None:
        self.processing_updates.append(
            {
                "request_id": job.request_id,
                "piecerate_job_id": piecerate_job_id,
                "piecerate_request_id": piecerate_request_id,
            }
        )

    def refresh_job_lock(
        self,
        job: JudgeApiJob,
        *,
        status: str,
        piecerate_job_id: str | None,
        piecerate_request_id: str | None,
    ) -> None:
        self.refresh_updates.append(
            {
                "request_id": job.request_id,
                "status": status,
                "piecerate_job_id": piecerate_job_id,
                "piecerate_request_id": piecerate_request_id,
            }
        )

    def upsert_result(self, payload: dict[str, object]) -> None:
        self.upserted_results.append(payload)

    def mark_job_completed(
        self,
        job: JudgeApiJob,
        *,
        piecerate_job_id: str | None,
        piecerate_request_id: str | None,
    ) -> None:
        self.completed_updates.append(
            {
                "request_id": job.request_id,
                "piecerate_job_id": piecerate_job_id,
                "piecerate_request_id": piecerate_request_id,
            }
        )

    def mark_job_failed(
        self,
        job: JudgeApiJob,
        *,
        last_error: str,
        piecerate_job_id: str | None,
        piecerate_request_id: str | None,
    ) -> None:
        self.failed_updates.append(
            {
                "request_id": job.request_id,
                "last_error": last_error,
                "piecerate_job_id": piecerate_job_id,
                "piecerate_request_id": piecerate_request_id,
            }
        )

    def finalize_job_from_existing_result(self, job: JudgeApiJob, existing: JudgeApiResultRecord) -> None:
        self.finalized_existing.append((job, existing))

    def requeue_job(self, job: JudgeApiJob, *, last_error: str) -> None:
        self.requeued_updates.append({"request_id": job.request_id, "last_error": last_error})

    def delete_input_object(self, job: JudgeApiJob) -> None:
        self.deleted_inputs.append(job)

    def get_next_pending_retry_at(self) -> datetime | None:
        if not self.next_retry_values:
            return None
        return self.next_retry_values.pop(0)


class FakePiecerateClient:
    def __init__(
        self,
        *,
        submission: PiecerateSubmission | Exception | None = None,
        statuses: list[PiecerateStatusResponse | Exception] | None = None,
    ) -> None:
        self.submission = submission or PiecerateSubmission(
            job_id="piecerate-job-1",
            request_id="piecerate-request-1",
            http_status=202,
            payload={"job_id": "piecerate-job-1", "request_id": "piecerate-request-1", "status": "queued"},
        )
        self.statuses = list(statuses or [])
        self.submit_calls: list[dict[str, object]] = []
        self.status_calls: list[str] = []
        self.closed = False

    def submit_prediction(self, raw_bytes: bytes, *, filename: str | None):
        self.submit_calls.append({"raw_bytes": raw_bytes, "filename": filename})
        if isinstance(self.submission, Exception):
            raise self.submission
        return self.submission

    def get_prediction_status(self, piecerate_job_id: str):
        self.status_calls.append(piecerate_job_id)
        if not self.statuses:
            raise AssertionError("No fake Piecerate status response queued.")
        next_value = self.statuses.pop(0)
        if isinstance(next_value, Exception):
            raise next_value
        return next_value

    def close(self) -> None:
        self.closed = True


class FakeRealtimeChannel:
    def __init__(self) -> None:
        self.callbacks: dict[str, object] = {}
        self.subscribe_callback = None
        self.subscribe_called = asyncio.Event()

    def on_postgres_changes(
        self,
        event,
        callback,
        table: str = "*",
        schema: str = "public",
        filter: str | None = None,
    ):
        self.callbacks[event] = callback
        return self

    async def subscribe(self, callback):
        self.subscribe_callback = callback
        self.subscribe_called.set()
        return self

    def emit_status(self, status: str, err: Exception | None = None) -> None:
        if self.subscribe_callback is None:
            raise AssertionError("Channel was not subscribed.")
        self.subscribe_callback(status, err)

    def emit_change(self, event: str, payload: dict[str, object]) -> None:
        callback = self.callbacks.get(event)
        if callback is None:
            raise AssertionError(f"No callback registered for {event}.")
        callback(payload)


class FakeRealtimeClient:
    def __init__(self) -> None:
        self.realtime = self
        self.channel_instance = FakeRealtimeChannel()
        self.listen_started = asyncio.Event()
        self.listen_stopped = asyncio.Event()
        self.channel_topics: list[str] = []
        self.removed_channels: list[FakeRealtimeChannel] = []

    def channel(self, topic: str):
        self.channel_topics.append(topic)
        return self.channel_instance

    async def remove_channel(self, channel) -> None:
        self.removed_channels.append(channel)
        self.listen_stopped.set()

    async def listen(self) -> None:
        self.listen_started.set()
        await self.listen_stopped.wait()


def build_completed_status_response(*, score: int = 7) -> PiecerateStatusResponse:
    return PiecerateStatusResponse(
        job_id="piecerate-job-1",
        request_id="piecerate-request-1",
        status="completed",
        http_status=200,
        payload={
            "job_id": "piecerate-job-1",
            "request_id": "piecerate-request-1",
            "status": "completed",
            "result": {
                "image_usable": True,
                "medium": "wall_piece",
                "overall_score": score,
                "legibility": score,
                "letter_structure": score,
                "line_quality": score,
                "composition": score,
                "color_harmony": score,
                "originality": score,
            },
        },
    )


class JudgeApiHandoffRuntimeTests(unittest.TestCase):
    def test_from_env_rejects_invalid_lock_refresh_seconds(self) -> None:
        for raw_value in ("0", "301"):
            with self.subTest(raw_value=raw_value):
                with patch.dict(
                    "os.environ",
                    {
                        "SUPABASE_URL": "https://example.supabase.co",
                        "SUPABASE_SERVICE_ROLE_KEY": "service-role-key",
                        "JUDGE_API_TOKEN": "judge-token",
                        "JUDGE_JOB_LOCK_TIMEOUT_SECONDS": "600",
                        "JUDGE_JOB_LOCK_REFRESH_SECONDS": raw_value,
                    },
                    clear=True,
                ):
                    with self.assertRaises(ValueError):
                        JudgeApiHandoffConfig.from_env()

    def test_claim_next_job_returns_none_when_rpc_returns_no_rows(self) -> None:
        runtime = SupabaseJudgeApiRuntime(build_config(), supabase_client=FakeSupabaseClaimClient([]))
        self.assertIsNone(runtime.claim_next_job())

    def test_claim_next_job_normalizes_fresh_pending_row(self) -> None:
        row = {
            "request_id": "request-1",
            "status": "claimed",
            "input_storage_bucket": "judge-api-inputs",
            "input_storage_path": "inputs/request-1.png",
            "filename": "piece.png",
            "worker_attempt_count": 1,
        }
        runtime = SupabaseJudgeApiRuntime(build_config(), supabase_client=FakeSupabaseClaimClient([row]))
        job = runtime.claim_next_job()

        assert job is not None
        self.assertEqual(job.request_id, "request-1")
        self.assertEqual(job.status, "claimed")
        self.assertEqual(job.worker_attempt_count, 1)

    def test_claim_next_job_accepts_stale_claimed_row(self) -> None:
        row = {
            "request_id": "request-claimed",
            "status": "claimed",
            "input_storage_bucket": "judge-api-inputs",
            "input_storage_path": "inputs/request-claimed.png",
            "filename": "piece.png",
            "worker_attempt_count": 3,
            "locked_at": "2026-03-29T18:00:00+00:00",
        }
        runtime = SupabaseJudgeApiRuntime(build_config(), supabase_client=FakeSupabaseClaimClient([row]))
        job = runtime.claim_next_job()

        assert job is not None
        self.assertEqual(job.status, "claimed")
        self.assertEqual(job.worker_attempt_count, 3)

    def test_claim_next_job_accepts_stale_processing_row(self) -> None:
        row = {
            "request_id": "request-processing",
            "status": "processing",
            "input_storage_bucket": "judge-api-inputs",
            "input_storage_path": "inputs/request-processing.png",
            "filename": "piece.png",
            "worker_attempt_count": 2,
            "piecerate_job_id": "piecerate-job-1",
            "piecerate_request_id": "piecerate-request-1",
            "locked_at": "2026-03-29T18:00:00+00:00",
        }
        runtime = SupabaseJudgeApiRuntime(build_config(), supabase_client=FakeSupabaseClaimClient([row]))
        job = runtime.claim_next_job()

        assert job is not None
        self.assertEqual(job.status, "processing")
        self.assertEqual(job.piecerate_job_id, "piecerate-job-1")

    def test_claim_next_job_falls_back_when_claim_rpc_is_missing(self) -> None:
        class FakeRpcMissingClient:
            def rpc(self, name, params):
                self.name = name
                self.params = params
                return self

            def execute(self):
                raise Exception(
                    "{'message': 'Could not find the function public.claim_next_judge_api_job(...)', 'code': 'PGRST202'}"
                )

        runtime = SupabaseJudgeApiRuntime(build_config(), supabase_client=FakeRpcMissingClient())
        expected = build_job()
        runtime._claim_next_job_via_table_fallback = Mock(return_value=expected)  # type: ignore[method-assign]

        job = runtime.claim_next_job()

        self.assertEqual(job, expected)
        runtime._claim_next_job_via_table_fallback.assert_called_once()

    def test_claim_job_candidate_sets_started_at_for_fresh_pending_jobs(self) -> None:
        pending_job = build_job(
            status="pending",
            worker_attempt_count=0,
            started_at=None,
            piecerate_job_id=None,
            piecerate_request_id=None,
        )
        client = FakeMutableJobsClient(build_job_row(pending_job))
        runtime = SupabaseJudgeApiRuntime(build_config(), supabase_client=client)

        claimed_job = runtime._claim_job_candidate(pending_job)

        assert claimed_job is not None
        payload = client.jobs.update_payloads[-1]
        self.assertIn("started_at", payload)
        self.assertEqual(payload["started_at"], claimed_job.started_at)
        self.assertEqual(claimed_job.created_at, pending_job.created_at)
        self.assertEqual(claimed_job.worker_attempt_count, 1)

    def test_claim_job_candidate_preserves_started_at_for_retries_and_reclaims(self) -> None:
        for status in ("pending", JUDGE_JOB_STATUS_CLAIMED, JUDGE_JOB_STATUS_PROCESSING):
            with self.subTest(status=status):
                candidate = build_job(
                    status=status,
                    worker_attempt_count=2,
                    started_at="2026-03-29T19:00:00+00:00",
                    piecerate_job_id="piecerate-job-1" if status == JUDGE_JOB_STATUS_PROCESSING else None,
                    piecerate_request_id="piecerate-request-1" if status == JUDGE_JOB_STATUS_PROCESSING else None,
                )
                client = FakeMutableJobsClient(build_job_row(candidate))
                runtime = SupabaseJudgeApiRuntime(build_config(), supabase_client=client)

                claimed_job = runtime._claim_job_candidate(candidate)

                assert claimed_job is not None
                payload = client.jobs.update_payloads[-1]
                self.assertNotIn("started_at", payload)
                self.assertEqual(claimed_job.started_at, candidate.started_at)
                self.assertEqual(claimed_job.worker_attempt_count, candidate.worker_attempt_count + 1)

    def test_requeue_job_preserves_started_at(self) -> None:
        job = build_job(status="pending", worker_attempt_count=2)
        client = FakeMutableJobsClient(build_job_row(job))
        runtime = SupabaseJudgeApiRuntime(build_config(), supabase_client=client)

        runtime.requeue_job(job, last_error="temporary failure")

        payload = client.jobs.update_payloads[-1]
        self.assertNotIn("started_at", payload)
        self.assertEqual(client.jobs.row["started_at"], job.started_at)

    def test_job_lifecycle_keeps_created_started_completed_order(self) -> None:
        pending_job = build_job(
            status="pending",
            worker_attempt_count=0,
            created_at="2026-03-29T18:55:00+00:00",
            started_at=None,
            piecerate_job_id=None,
            piecerate_request_id=None,
        )
        client = FakeMutableJobsClient(build_job_row(pending_job))
        runtime = SupabaseJudgeApiRuntime(build_config(), supabase_client=client)

        claimed_job = runtime._claim_job_candidate(pending_job)
        assert claimed_job is not None
        runtime.mark_job_completed(
            claimed_job,
            piecerate_job_id="piecerate-job-1",
            piecerate_request_id="piecerate-request-1",
        )

        created_at = client.jobs.row["created_at"]
        started_at = client.jobs.row["started_at"]
        completed_at = client.jobs.row["completed_at"]
        self.assertIsNotNone(started_at)
        self.assertIsNotNone(completed_at)
        self.assertLessEqual(created_at, started_at)
        self.assertLessEqual(started_at, completed_at)

    def test_mark_job_failed_preserves_started_at(self) -> None:
        job = build_job(status=JUDGE_JOB_STATUS_PROCESSING)
        client = FakeMutableJobsClient(build_job_row(job))
        runtime = SupabaseJudgeApiRuntime(build_config(), supabase_client=client)

        runtime.mark_job_failed(
            job,
            last_error="terminal failure",
            piecerate_job_id=job.piecerate_job_id,
            piecerate_request_id=job.piecerate_request_id,
        )

        payload = client.jobs.update_payloads[-1]
        self.assertNotIn("started_at", payload)
        self.assertEqual(client.jobs.row["started_at"], job.started_at)
        self.assertIsNotNone(client.jobs.row["completed_at"])


class JudgeApiHandoffWorkerTests(unittest.TestCase):
    def test_process_handoff_job_submits_then_completes(self) -> None:
        config = build_config()
        runtime = FakeRuntime(
            config,
            claimed_jobs=[build_job()],
        )
        piecerate = FakePiecerateClient(
            statuses=[
                PiecerateStatusResponse(
                    job_id="piecerate-job-1",
                    request_id="piecerate-request-1",
                    status="queued",
                    http_status=200,
                    payload={"job_id": "piecerate-job-1", "request_id": "piecerate-request-1", "status": "queued"},
                ),
                PiecerateStatusResponse(
                    job_id="piecerate-job-1",
                    request_id="piecerate-request-1",
                    status="completed",
                    http_status=200,
                    payload={
                        "job_id": "piecerate-job-1",
                        "request_id": "piecerate-request-1",
                        "status": "completed",
                        "model_version": "student-v2-dinov2",
                        "result": {
                            "image_usable": True,
                            "medium": "wall_piece",
                            "overall_score": 7,
                            "legibility": 6,
                            "letter_structure": 7,
                            "line_quality": 8,
                            "composition": 7,
                            "color_harmony": 6,
                            "originality": 8,
                            "request_id": "piecerate-request-1",
                            "model_version": "student-v2-dinov2",
                        },
                    },
                ),
            ]
        )

        processed = run_worker_iteration(runtime, piecerate, config)

        self.assertEqual(processed, True)
        self.assertEqual(len(runtime.download_calls), 1)
        self.assertEqual(len(piecerate.submit_calls), 1)
        self.assertEqual(len(runtime.processing_updates), 1)
        self.assertEqual(len(runtime.refresh_updates), 0)
        self.assertEqual(len(runtime.upserted_results), 1)
        self.assertEqual(runtime.upserted_results[0]["overall_score"], 7)
        source_image = runtime.upserted_results[0]["response_payload"]["source_image"]
        self.assertEqual(source_image["storage_kind"], "local_disk")
        self.assertEqual(source_image["original_filename"], "piece.png")
        self.assertIn("request-1", source_image["local_path"])
        self.assertEqual(len(runtime.completed_updates), 1)
        self.assertEqual(len(runtime.archived_inputs), 1)
        self.assertEqual(len(runtime.deleted_inputs), 1)

    def test_process_handoff_job_throttles_lock_refreshes_for_fresh_jobs(self) -> None:
        config = build_config()
        runtime = FakeRuntime(config, claimed_jobs=[build_job()])
        piecerate = FakePiecerateClient(
            statuses=[
                PiecerateStatusResponse(
                    job_id="piecerate-job-1",
                    request_id="piecerate-request-1",
                    status="queued",
                    http_status=200,
                    payload={"job_id": "piecerate-job-1", "request_id": "piecerate-request-1", "status": "queued"},
                ),
                PiecerateStatusResponse(
                    job_id="piecerate-job-1",
                    request_id="piecerate-request-1",
                    status="processing",
                    http_status=200,
                    payload={
                        "job_id": "piecerate-job-1",
                        "request_id": "piecerate-request-1",
                        "status": "processing",
                    },
                ),
                build_completed_status_response(score=7),
            ]
        )
        base_time = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)

        with patch(
            "deploy.judge_api_handoff_worker._utc_now",
            side_effect=[
                base_time,
                base_time + timedelta(seconds=30),
                base_time + timedelta(seconds=90),
            ],
        ):
            process_handoff_job(runtime, piecerate, config, build_job())

        self.assertEqual(len(runtime.processing_updates), 1)
        self.assertEqual(len(runtime.refresh_updates), 0)
        self.assertEqual(len(runtime.completed_updates), 1)

    def test_process_handoff_job_refreshes_lock_once_threshold_elapses(self) -> None:
        config = build_config()
        runtime = FakeRuntime(config, claimed_jobs=[build_job()])
        piecerate = FakePiecerateClient(
            statuses=[
                PiecerateStatusResponse(
                    job_id="piecerate-job-1",
                    request_id="piecerate-request-1",
                    status="queued",
                    http_status=200,
                    payload={"job_id": "piecerate-job-1", "request_id": "piecerate-request-1", "status": "queued"},
                ),
                PiecerateStatusResponse(
                    job_id="piecerate-job-1",
                    request_id="piecerate-request-1",
                    status="queued",
                    http_status=200,
                    payload={"job_id": "piecerate-job-1", "request_id": "piecerate-request-1", "status": "queued"},
                ),
                build_completed_status_response(score=8),
            ]
        )
        base_time = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)

        with patch(
            "deploy.judge_api_handoff_worker._utc_now",
            side_effect=[
                base_time,
                base_time + timedelta(seconds=30),
                base_time + timedelta(seconds=130),
            ],
        ):
            process_handoff_job(runtime, piecerate, config, build_job())

        self.assertEqual(len(runtime.processing_updates), 1)
        self.assertEqual(len(runtime.refresh_updates), 1)
        self.assertEqual(runtime.refresh_updates[0]["status"], JUDGE_JOB_STATUS_PROCESSING)
        self.assertEqual(len(runtime.completed_updates), 1)

    def test_process_handoff_job_refreshes_reclaimed_processing_immediately_when_stale(self) -> None:
        config = build_config()
        base_time = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)
        stale_job = build_job(
            status=JUDGE_JOB_STATUS_PROCESSING,
            worker_attempt_count=2,
            locked_at=(base_time - timedelta(seconds=121)).isoformat(),
            piecerate_job_id="piecerate-job-1",
            piecerate_request_id="piecerate-request-1",
        )
        runtime = FakeRuntime(config, claimed_jobs=[stale_job])
        piecerate = FakePiecerateClient(
            statuses=[
                PiecerateStatusResponse(
                    job_id="piecerate-job-1",
                    request_id="piecerate-request-1",
                    status="queued",
                    http_status=200,
                    payload={"job_id": "piecerate-job-1", "request_id": "piecerate-request-1", "status": "queued"},
                ),
                build_completed_status_response(score=8),
            ]
        )

        with patch("deploy.judge_api_handoff_worker._utc_now", return_value=base_time):
            process_handoff_job(runtime, piecerate, config, stale_job)

        self.assertEqual(len(piecerate.submit_calls), 0)
        self.assertEqual(len(runtime.processing_updates), 0)
        self.assertEqual(len(runtime.refresh_updates), 1)
        self.assertEqual(runtime.refresh_updates[0]["piecerate_job_id"], "piecerate-job-1")
        self.assertEqual(len(runtime.completed_updates), 1)

    def test_process_handoff_job_short_circuits_existing_result(self) -> None:
        config = build_config()
        existing = JudgeApiResultRecord(
            request_id="request-1",
            status=JUDGE_JOB_STATUS_COMPLETED,
            judge_api_job_id="existing-job",
            judge_api_request_id="existing-request",
            judge_api_model_version="student-v2-dinov2",
            judge_api_http_status=200,
            response_payload={"status": "completed"},
            error_payload=None,
        )
        job = build_job()
        runtime = FakeRuntime(config, claimed_jobs=[job], existing_result=existing)
        piecerate = FakePiecerateClient(statuses=[])

        processed = run_worker_iteration(runtime, piecerate, config)

        self.assertEqual(processed, True)
        self.assertEqual(len(runtime.finalized_existing), 1)
        self.assertEqual(len(runtime.ensured_archives), 1)
        self.assertEqual(len(runtime.deleted_inputs), 1)
        self.assertEqual(len(runtime.download_calls), 0)
        self.assertEqual(len(piecerate.submit_calls), 0)

    def test_process_handoff_job_ignores_non_terminal_existing_result(self) -> None:
        config = build_config()
        existing = JudgeApiResultRecord(
            request_id="request-1",
            status="queued",
            judge_api_job_id="existing-job",
            judge_api_request_id="existing-request",
            judge_api_model_version="student-v2-dinov2",
            judge_api_http_status=202,
            response_payload={"status": "queued"},
            error_payload=None,
        )
        runtime = FakeRuntime(config, claimed_jobs=[build_job()], existing_result=existing)
        piecerate = FakePiecerateClient(
            statuses=[
                PiecerateStatusResponse(
                    job_id="piecerate-job-1",
                    request_id="piecerate-request-1",
                    status="completed",
                    http_status=200,
                    payload={
                        "job_id": "piecerate-job-1",
                        "request_id": "piecerate-request-1",
                        "status": "completed",
                        "result": {
                            "image_usable": True,
                            "medium": "paper_sketch",
                            "overall_score": 7,
                            "legibility": 7,
                            "letter_structure": 7,
                            "line_quality": 7,
                            "composition": 7,
                            "color_harmony": 7,
                            "originality": 7,
                        },
                    },
                )
            ]
        )

        processed = run_worker_iteration(runtime, piecerate, config)

        self.assertEqual(processed, True)
        self.assertEqual(len(runtime.finalized_existing), 0)
        self.assertEqual(len(runtime.download_calls), 1)
        self.assertEqual(len(piecerate.submit_calls), 1)
        self.assertEqual(len(runtime.upserted_results), 1)
        self.assertEqual(len(runtime.completed_updates), 1)

    def test_process_handoff_job_marks_terminal_failure_from_poll(self) -> None:
        config = build_config()
        runtime = FakeRuntime(config, claimed_jobs=[build_job()])
        piecerate = FakePiecerateClient(
            statuses=[
                PiecerateStatusResponse(
                    job_id="piecerate-job-1",
                    request_id="piecerate-request-1",
                    status="failed",
                    http_status=200,
                    payload={
                        "job_id": "piecerate-job-1",
                        "request_id": "piecerate-request-1",
                        "status": "failed",
                        "error": "invalid_image",
                        "message": "The uploaded content is not a valid image.",
                    },
                )
            ]
        )

        process_handoff_job(runtime, piecerate, config, build_job())

        self.assertEqual(len(runtime.failed_updates), 1)
        self.assertEqual(runtime.failed_updates[0]["last_error"], "The uploaded content is not a valid image.")
        self.assertEqual(len(runtime.refresh_updates), 0)
        self.assertEqual(len(runtime.upserted_results), 1)
        error_payload = runtime.upserted_results[0]["error_payload"]
        self.assertIsNotNone(error_payload)
        self.assertEqual(error_payload["source_image"]["storage_kind"], "local_disk")
        self.assertEqual(error_payload["source_image"]["original_filename"], "piece.png")
        self.assertEqual(len(runtime.deleted_inputs), 1)

    def test_process_handoff_job_requeues_transient_submit_failures(self) -> None:
        config = build_config()
        runtime = FakeRuntime(config, claimed_jobs=[build_job()])
        piecerate = FakePiecerateClient(submission=RetryableWorkerError("Piecerate request timed out."))

        process_handoff_job(runtime, piecerate, config, build_job())

        self.assertEqual(len(runtime.requeued_updates), 1)
        self.assertIn("timed out", runtime.requeued_updates[0]["last_error"])
        self.assertEqual(len(runtime.upserted_results), 0)
        self.assertEqual(len(runtime.archived_inputs), 1)
        self.assertEqual(len(runtime.deleted_inputs), 0)

    def test_process_handoff_job_resumes_stale_processing_without_resubmitting(self) -> None:
        config = build_config()
        stale_job = build_job(
            status=JUDGE_JOB_STATUS_PROCESSING,
            worker_attempt_count=2,
            piecerate_job_id="piecerate-job-1",
            piecerate_request_id="piecerate-request-1",
        )
        runtime = FakeRuntime(config, claimed_jobs=[stale_job])
        piecerate = FakePiecerateClient(
            statuses=[
                PiecerateStatusResponse(
                    job_id="piecerate-job-1",
                    request_id="piecerate-request-1",
                    status="completed",
                    http_status=200,
                    payload={
                        "job_id": "piecerate-job-1",
                        "request_id": "piecerate-request-1",
                        "status": "completed",
                        "result": {
                            "image_usable": True,
                            "medium": "paper_sketch",
                            "overall_score": 8,
                            "legibility": 8,
                            "letter_structure": 8,
                            "line_quality": 8,
                            "composition": 7,
                            "color_harmony": None,
                            "originality": 8,
                        },
                    },
                )
            ]
        )

        process_handoff_job(runtime, piecerate, config, stale_job)

        self.assertEqual(len(runtime.download_calls), 0)
        self.assertEqual(len(piecerate.submit_calls), 0)
        self.assertEqual(len(runtime.completed_updates), 1)
        self.assertEqual(runtime.completed_updates[0]["piecerate_job_id"], "piecerate-job-1")
        self.assertEqual(len(runtime.ensured_archives), 1)
        self.assertEqual(
            runtime.upserted_results[0]["response_payload"]["source_image"]["storage_kind"],
            "local_disk",
        )

    def test_retryable_failure_can_recover_on_later_iteration(self) -> None:
        config = build_config()
        first_attempt = build_job(worker_attempt_count=1)
        second_attempt = replace(first_attempt, worker_attempt_count=2)
        runtime = FakeRuntime(config, claimed_jobs=[first_attempt, second_attempt])
        piecerate = FakePiecerateClient(
            submission=PiecerateSubmission(
                job_id="piecerate-job-1",
                request_id="piecerate-request-1",
                http_status=202,
                payload={"job_id": "piecerate-job-1", "request_id": "piecerate-request-1", "status": "queued"},
            ),
            statuses=[
                RetryableWorkerError("Temporary network drop."),
                PiecerateStatusResponse(
                    job_id="piecerate-job-1",
                    request_id="piecerate-request-1",
                    status="completed",
                    http_status=200,
                    payload={
                        "job_id": "piecerate-job-1",
                        "request_id": "piecerate-request-1",
                        "status": "completed",
                        "result": {
                            "image_usable": True,
                            "medium": "wall_piece",
                            "overall_score": 9,
                            "legibility": 9,
                            "letter_structure": 9,
                            "line_quality": 9,
                            "composition": 8,
                            "color_harmony": 8,
                            "originality": 9,
                        },
                    },
                ),
            ],
        )

        self.assertEqual(run_worker_iteration(runtime, piecerate, config), True)
        self.assertEqual(run_worker_iteration(runtime, piecerate, config), True)

        self.assertEqual(len(runtime.requeued_updates), 1)
        self.assertEqual(len(runtime.completed_updates), 1)
        self.assertEqual(runtime.upserted_results[-1]["overall_score"], 9)
        self.assertEqual(runtime.upserted_results[-1]["response_payload"]["source_image"]["storage_kind"], "local_disk")


class JudgeApiHandoffCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def _start_worker(
        self,
        runtime: FakeRuntime,
        *,
        config: JudgeApiHandoffConfig | None = None,
        piecerate: FakePiecerateClient | None = None,
    ) -> tuple[asyncio.Event, asyncio.Task[None], FakeRealtimeClient, FakePiecerateClient]:
        worker_config = config or build_config()
        realtime_client = FakeRealtimeClient()
        piecerate_client = piecerate or FakePiecerateClient(statuses=[build_completed_status_response()])

        async def factory(_: JudgeApiHandoffConfig) -> FakeRealtimeClient:
            return realtime_client

        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_worker_service(
                worker_config,
                stop_event=stop_event,
                runtime=runtime,  # type: ignore[arg-type]
                piecerate=piecerate_client,  # type: ignore[arg-type]
                realtime_client_factory=factory,
            )
        )
        await asyncio.wait_for(realtime_client.channel_instance.subscribe_called.wait(), timeout=1)
        await asyncio.wait_for(realtime_client.listen_started.wait(), timeout=1)
        return stop_event, task, realtime_client, piecerate_client

    async def _stop_worker(
        self,
        stop_event: asyncio.Event,
        task: asyncio.Task[None],
        realtime_client: FakeRealtimeClient,
    ) -> None:
        stop_event.set()
        realtime_client.listen_stopped.set()
        await asyncio.wait_for(task, timeout=1)

    async def _wait_for(self, predicate, *, timeout: float = 1.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        self.fail("Timed out waiting for condition.")

    async def test_startup_catch_up_drain_processes_existing_job_without_idle_polling(self) -> None:
        config = build_config()
        runtime = FakeRuntime(
            config,
            claimed_jobs=[build_job()],
            next_retry_values=[None],
        )
        piecerate = FakePiecerateClient(statuses=[build_completed_status_response(score=8)])

        stop_event, task, realtime_client, _ = await self._start_worker(runtime, config=config, piecerate=piecerate)
        try:
            await self._wait_for(lambda: len(runtime.completed_updates) == 1)
            baseline_claim_calls = runtime.claim_calls
            await asyncio.sleep(0.05)
            self.assertEqual(runtime.claim_calls, baseline_claim_calls)
            self.assertEqual(runtime.claim_calls, 2)
        finally:
            await self._stop_worker(stop_event, task, realtime_client)

    async def test_realtime_insert_pending_wakes_worker_and_drains_claims(self) -> None:
        config = build_config()
        runtime = FakeRuntime(config, next_retry_values=[None, None])
        piecerate = FakePiecerateClient(statuses=[build_completed_status_response(score=6)])

        stop_event, task, realtime_client, _ = await self._start_worker(runtime, config=config, piecerate=piecerate)
        try:
            runtime.claimed_jobs.append(build_job())
            realtime_client.channel_instance.emit_change(
                "INSERT",
                {"record": {"status": JUDGE_JOB_STATUS_PENDING, "next_attempt_at": None}},
            )
            await self._wait_for(lambda: len(runtime.completed_updates) == 1)
            self.assertGreaterEqual(runtime.claim_calls, 3)
        finally:
            await self._stop_worker(stop_event, task, realtime_client)

    async def test_realtime_update_pending_future_retry_schedules_timer_without_immediate_claim(self) -> None:
        config = build_config()
        runtime = FakeRuntime(config, next_retry_values=[None, None])
        piecerate = FakePiecerateClient(statuses=[build_completed_status_response(score=5)])

        stop_event, task, realtime_client, _ = await self._start_worker(runtime, config=config, piecerate=piecerate)
        try:
            baseline_claim_calls = runtime.claim_calls
            runtime.claimed_jobs.append(build_job())
            future_retry_at = datetime.now(timezone.utc) + timedelta(milliseconds=80)
            realtime_client.channel_instance.emit_change(
                "UPDATE",
                {"record": {"status": JUDGE_JOB_STATUS_PENDING, "next_attempt_at": future_retry_at.isoformat()}},
            )
            await asyncio.sleep(0.03)
            self.assertEqual(runtime.claim_calls, baseline_claim_calls)
            await self._wait_for(lambda: len(runtime.completed_updates) == 1)
            self.assertGreater(runtime.claim_calls, baseline_claim_calls)
        finally:
            await self._stop_worker(stop_event, task, realtime_client)

    async def test_realtime_subscribed_status_triggers_catch_up_drain_on_reconnect(self) -> None:
        config = build_config()
        runtime = FakeRuntime(config, next_retry_values=[None, None, None])
        piecerate = FakePiecerateClient(
            statuses=[
                build_completed_status_response(score=7),
                build_completed_status_response(score=9),
            ]
        )

        stop_event, task, realtime_client, _ = await self._start_worker(runtime, config=config, piecerate=piecerate)
        try:
            runtime.claimed_jobs.append(build_job())
            realtime_client.channel_instance.emit_status("SUBSCRIBED")
            await self._wait_for(lambda: len(runtime.completed_updates) == 1)
            baseline_claim_calls = runtime.claim_calls
            runtime.claimed_jobs.append(build_job())
            realtime_client.channel_instance.emit_status("SUBSCRIBED")
            await self._wait_for(lambda: len(runtime.completed_updates) == 2)
            self.assertGreater(runtime.claim_calls, baseline_claim_calls)
        finally:
            await self._stop_worker(stop_event, task, realtime_client)

    async def test_safety_sweep_recovers_missed_activation(self) -> None:
        config = replace(build_config(), safety_sweep_seconds=1)
        runtime = FakeRuntime(config, next_retry_values=[None, None])
        piecerate = FakePiecerateClient(statuses=[build_completed_status_response(score=4)])

        stop_event, task, realtime_client, _ = await self._start_worker(runtime, config=config, piecerate=piecerate)
        try:
            runtime.claimed_jobs.append(build_job())
            await self._wait_for(lambda: len(runtime.completed_updates) == 1, timeout=1.5)
            self.assertGreaterEqual(runtime.claim_calls, 3)
        finally:
            await self._stop_worker(stop_event, task, realtime_client)

    async def test_duplicate_events_are_coalesced_into_one_drain(self) -> None:
        config = build_config()
        runtime = FakeRuntime(config, next_retry_values=[None, None])

        stop_event, task, realtime_client, _ = await self._start_worker(runtime, config=config)
        try:
            baseline_claim_calls = runtime.claim_calls
            realtime_client.channel_instance.emit_change(
                "INSERT",
                {"record": {"status": JUDGE_JOB_STATUS_PENDING, "next_attempt_at": None}},
            )
            realtime_client.channel_instance.emit_change(
                "INSERT",
                {"record": {"status": JUDGE_JOB_STATUS_PENDING, "next_attempt_at": None}},
            )
            await self._wait_for(lambda: runtime.claim_calls == baseline_claim_calls + 1)
            await asyncio.sleep(0.05)
            self.assertEqual(runtime.claim_calls, baseline_claim_calls + 1)
        finally:
            await self._stop_worker(stop_event, task, realtime_client)

    async def test_idle_worker_does_not_keep_claiming_between_events(self) -> None:
        config = replace(build_config(), safety_sweep_seconds=60)
        runtime = FakeRuntime(config, next_retry_values=[None])

        stop_event, task, realtime_client, _ = await self._start_worker(runtime, config=config)
        try:
            baseline_claim_calls = runtime.claim_calls
            await asyncio.sleep(0.1)
            self.assertEqual(runtime.claim_calls, baseline_claim_calls)
            self.assertEqual(runtime.claim_calls, 1)
        finally:
            await self._stop_worker(stop_event, task, realtime_client)


if __name__ == "__main__":
    unittest.main()
