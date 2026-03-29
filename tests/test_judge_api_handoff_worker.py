from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from deploy.judge_api_handoff_runtime import (
    ArchivedJudgeImage,
    JUDGE_JOB_STATUS_CLAIMED,
    JUDGE_JOB_STATUS_COMPLETED,
    JUDGE_JOB_STATUS_FAILED,
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
        poll_wait_ms=8_000,
        idle_sleep_seconds=0.01,
        max_attempts=5,
        backoff_schedule_seconds=(30, 120, 600, 600),
    )


def build_job(
    *,
    status: str = JUDGE_JOB_STATUS_CLAIMED,
    worker_attempt_count: int = 1,
    piecerate_job_id: str | None = None,
    piecerate_request_id: str | None = None,
) -> JudgeApiJob:
    return JudgeApiJob(
        request_id="request-1",
        status=status,
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
        locked_at="2026-03-29T19:00:00+00:00",
        locked_by="worker-1",
        piecerate_job_id=piecerate_job_id,
        piecerate_request_id=piecerate_request_id,
        last_error=None,
        completed_at=None,
    )


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


class FakeRuntime:
    def __init__(
        self,
        config: JudgeApiHandoffConfig,
        *,
        claimed_jobs: list[JudgeApiJob] | None = None,
        existing_result: JudgeApiResultRecord | None = None,
    ) -> None:
        self.config = config
        self.claimed_jobs = list(claimed_jobs or [])
        self.existing_result = existing_result
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


class JudgeApiHandoffRuntimeTests(unittest.TestCase):
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
        self.assertEqual(len(runtime.refresh_updates), 1)
        self.assertEqual(len(runtime.upserted_results), 1)
        self.assertEqual(runtime.upserted_results[0]["overall_score"], 7)
        source_image = runtime.upserted_results[0]["response_payload"]["source_image"]
        self.assertEqual(source_image["storage_kind"], "local_disk")
        self.assertEqual(source_image["original_filename"], "piece.png")
        self.assertIn("request-1", source_image["local_path"])
        self.assertEqual(len(runtime.completed_updates), 1)
        self.assertEqual(len(runtime.archived_inputs), 1)
        self.assertEqual(len(runtime.deleted_inputs), 1)

    def test_process_handoff_job_short_circuits_existing_result(self) -> None:
        config = build_config()
        existing = JudgeApiResultRecord(
            request_id="request-1",
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


if __name__ == "__main__":
    unittest.main()
