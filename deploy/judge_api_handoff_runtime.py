from __future__ import annotations

import base64
import logging
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("graffiti_judge_handoff_runtime")


JUDGE_JOB_STATUS_PENDING = "pending"
JUDGE_JOB_STATUS_CLAIMED = "claimed"
JUDGE_JOB_STATUS_PROCESSING = "processing"
JUDGE_JOB_STATUS_COMPLETED = "completed"
JUDGE_JOB_STATUS_FAILED = "failed"
JUDGE_JOB_STATUS_CANCELLED = "cancelled"

PIECERATE_COMPLETED = "completed"
PIECERATE_FAILED = "failed"
PIECERATE_NON_TERMINAL_STATUSES = {"queued", "processing"}

DEFAULT_JUDGE_API_BASE_URL = "https://api.piecerate.me"
DEFAULT_JUDGE_API_TIMEOUT_MS = 30_000
DEFAULT_JOBS_TABLE = "judge_api_jobs"
DEFAULT_RESULTS_TABLE = "judge_api_results"
DEFAULT_INPUT_BUCKET = "judge-api-inputs"
DEFAULT_JUDGED_IMAGE_ARCHIVE_DIR = "/srv/graffiti-student/runtime/judged-images"
DEFAULT_LOCK_TIMEOUT_SECONDS = 600
DEFAULT_LOCK_REFRESH_SECONDS = 120
DEFAULT_POLL_WAIT_MS = 8_000
DEFAULT_IDLE_SLEEP_SECONDS = 1.0
DEFAULT_SAFETY_SWEEP_SECONDS = 600
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_SCHEDULE = (30, 120, 600, 600)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 "
    "PiecerateJudgeWorker/1.0"
)


class JudgeApiHandoffError(Exception):
    """Base class for handoff worker failures."""


class RetryableWorkerError(JudgeApiHandoffError):
    """Raised when the current job should be returned to the queue."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        payload: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.payload = payload


class TerminalJudgeApiError(JudgeApiHandoffError):
    """Raised when Piecerate returned a terminal failure for this request."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        payload: Any | None = None,
        piecerate_job_id: str | None = None,
        piecerate_request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.payload = payload
        self.piecerate_job_id = piecerate_job_id
        self.piecerate_request_id = piecerate_request_id


@dataclass(frozen=True)
class JudgeApiHandoffConfig:
    supabase_url: str
    supabase_service_role_key: str
    judge_api_token: str
    judge_api_base_url: str
    judge_api_timeout_ms: int
    jobs_table: str
    results_table: str
    input_bucket: str
    judged_image_archive_dir: Path
    worker_id: str
    lock_timeout_seconds: int
    lock_refresh_seconds: int
    poll_wait_ms: int
    idle_sleep_seconds: float
    safety_sweep_seconds: int
    max_attempts: int
    backoff_schedule_seconds: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.lock_refresh_seconds <= 0:
            raise ValueError("lock_refresh_seconds must be positive.")
        if self.lock_refresh_seconds > (self.lock_timeout_seconds / 2):
            raise ValueError("lock_refresh_seconds must be less than or equal to lock_timeout_seconds / 2.")

    @classmethod
    def from_env(cls) -> "JudgeApiHandoffConfig":
        return cls(
            supabase_url=_required_env("SUPABASE_URL"),
            supabase_service_role_key=_required_env("SUPABASE_SERVICE_ROLE_KEY"),
            judge_api_token=_required_env("JUDGE_API_TOKEN"),
            judge_api_base_url=(os.environ.get("JUDGE_API_BASE_URL") or DEFAULT_JUDGE_API_BASE_URL).rstrip("/"),
            judge_api_timeout_ms=_positive_int_env("JUDGE_API_TIMEOUT_MS", DEFAULT_JUDGE_API_TIMEOUT_MS),
            jobs_table=os.environ.get("SUPABASE_JUDGE_API_JOBS_TABLE") or DEFAULT_JOBS_TABLE,
            results_table=os.environ.get("SUPABASE_JUDGE_API_RESULTS_TABLE") or DEFAULT_RESULTS_TABLE,
            input_bucket=os.environ.get("SUPABASE_JUDGE_API_INPUT_BUCKET") or DEFAULT_INPUT_BUCKET,
            judged_image_archive_dir=Path(
                os.environ.get("JUDGED_IMAGE_ARCHIVE_DIR") or DEFAULT_JUDGED_IMAGE_ARCHIVE_DIR
            ),
            worker_id=os.environ.get("WORKER_ID") or build_worker_id(),
            lock_timeout_seconds=_positive_int_env(
                "JUDGE_JOB_LOCK_TIMEOUT_SECONDS",
                DEFAULT_LOCK_TIMEOUT_SECONDS,
            ),
            lock_refresh_seconds=_positive_int_env(
                "JUDGE_JOB_LOCK_REFRESH_SECONDS",
                DEFAULT_LOCK_REFRESH_SECONDS,
            ),
            poll_wait_ms=_positive_int_env("JUDGE_JOB_POLL_WAIT_MS", DEFAULT_POLL_WAIT_MS),
            idle_sleep_seconds=_ignored_positive_float_env(
                "JUDGE_JOB_IDLE_SLEEP_SECONDS",
                DEFAULT_IDLE_SLEEP_SECONDS,
            ),
            safety_sweep_seconds=_positive_int_env(
                "JUDGE_JOB_SAFETY_SWEEP_SECONDS",
                DEFAULT_SAFETY_SWEEP_SECONDS,
            ),
            max_attempts=_positive_int_env("JUDGE_JOB_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS),
            backoff_schedule_seconds=_parse_backoff_schedule(
                os.environ.get("JUDGE_JOB_BACKOFF_SCHEDULE_SECONDS"),
            ),
        )

    def retry_delay_seconds_for_attempt(self, worker_attempt_count: int) -> int:
        schedule_index = max(0, worker_attempt_count - 1)
        capped_index = min(schedule_index, len(self.backoff_schedule_seconds) - 1)
        return self.backoff_schedule_seconds[capped_index]


@dataclass(frozen=True)
class JudgeApiJob:
    request_id: str
    status: str
    created_at: str | None
    started_at: str | None
    input_storage_bucket: str | None
    input_storage_path: str
    filename: str | None
    image_mime_type: str | None
    image_size_bytes: int | None
    judge_image_hash_sha256: str | None
    base_image_hash_sha256: str | None
    llm_judgement_engine_id: str | None
    llm_model: str | None
    worker_attempt_count: int
    next_attempt_at: str | None
    locked_at: str | None
    locked_by: str | None
    piecerate_job_id: str | None
    piecerate_request_id: str | None
    last_error: str | None
    completed_at: str | None

    @property
    def input_bucket(self) -> str | None:
        return self.input_storage_bucket


@dataclass(frozen=True)
class JudgeApiResultRecord:
    request_id: str
    status: str
    judge_api_job_id: str | None
    judge_api_request_id: str | None
    judge_api_model_version: str | None
    judge_api_http_status: int | None
    response_payload: Any | None
    error_payload: Any | None

    @property
    def is_terminal(self) -> bool:
        return self.status in {JUDGE_JOB_STATUS_COMPLETED, JUDGE_JOB_STATUS_FAILED}

    @property
    def terminal_status(self) -> str:
        if self.status == JUDGE_JOB_STATUS_FAILED:
            return JUDGE_JOB_STATUS_FAILED
        return JUDGE_JOB_STATUS_COMPLETED


@dataclass(frozen=True)
class ArchivedJudgeImage:
    local_path: Path
    filename: str


@dataclass(frozen=True)
class PiecerateSubmission:
    job_id: str
    request_id: str | None
    http_status: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class PiecerateStatusResponse:
    job_id: str
    request_id: str | None
    status: str
    http_status: int
    payload: dict[str, Any]

    @property
    def is_terminal(self) -> bool:
        return self.status in {PIECERATE_COMPLETED, PIECERATE_FAILED}


class SupabaseJudgeApiRuntime:
    def __init__(
        self,
        config: JudgeApiHandoffConfig | None = None,
        *,
        supabase_client: Any | None = None,
    ) -> None:
        self.config = config or JudgeApiHandoffConfig.from_env()
        self.supabase = supabase_client or create_supabase_client(
            self.config.supabase_url,
            self.config.supabase_service_role_key,
        )
        self._claim_rpc_fallback_logged = False

    def claim_next_job(self) -> JudgeApiJob | None:
        try:
            response = self.supabase.rpc(
                "claim_next_judge_api_job",
                {
                    "p_worker_id": self.config.worker_id,
                    "p_lock_timeout_seconds": self.config.lock_timeout_seconds,
                },
            ).execute()
        except Exception as exc:
            if _is_missing_claim_rpc_error(exc):
                if not self._claim_rpc_fallback_logged:
                    logger.warning(
                        "Supabase RPC claim_next_judge_api_job is missing; falling back to table-based job claims."
                    )
                    self._claim_rpc_fallback_logged = True
                return self._claim_next_job_via_table_fallback()
            raise RetryableWorkerError(f"Supabase job claim failed: {exc}") from exc

        row = _first_row(getattr(response, "data", None))
        if row is None:
            return None
        return normalize_job_row(row)

    def _claim_next_job_via_table_fallback(self) -> JudgeApiJob | None:
        candidate = self._select_next_pending_job()
        if candidate is None:
            candidate = self._select_next_stale_job(JUDGE_JOB_STATUS_CLAIMED)
        if candidate is None:
            candidate = self._select_next_stale_job(JUDGE_JOB_STATUS_PROCESSING)
        if candidate is None:
            return None
        return self._claim_job_candidate(candidate)

    def _select_next_pending_job(self) -> JudgeApiJob | None:
        now_iso = utc_now_iso()
        try:
            response = (
                self.supabase.table(self.config.jobs_table)
                .select("*")
                .eq("status", JUDGE_JOB_STATUS_PENDING)
                .or_(f"next_attempt_at.is.null,next_attempt_at.lte.{now_iso}")
                .order("next_attempt_at")
                .limit(1)
                .execute()
            )
        except Exception as exc:
            raise RetryableWorkerError(f"Supabase pending job lookup failed: {exc}") from exc
        row = _first_row(getattr(response, "data", None))
        return None if row is None else normalize_job_row(row)

    def _select_next_stale_job(self, status: str) -> JudgeApiJob | None:
        stale_cutoff_iso = utc_in_seconds_iso(-self.config.lock_timeout_seconds)
        try:
            response = (
                self.supabase.table(self.config.jobs_table)
                .select("*")
                .eq("status", status)
                .lt("locked_at", stale_cutoff_iso)
                .order("locked_at")
                .limit(1)
                .execute()
            )
        except Exception as exc:
            raise RetryableWorkerError(f"Supabase stale job lookup failed: {exc}") from exc
        row = _first_row(getattr(response, "data", None))
        return None if row is None else normalize_job_row(row)

    def _claim_job_candidate(self, candidate: JudgeApiJob) -> JudgeApiJob | None:
        claimed_at = utc_now_iso()
        payload = {
            "status": JUDGE_JOB_STATUS_CLAIMED,
            "locked_at": claimed_at,
            "locked_by": self.config.worker_id,
            "worker_attempt_count": candidate.worker_attempt_count + 1,
            "updated_at": claimed_at,
        }
        if candidate.status == JUDGE_JOB_STATUS_PENDING and candidate.started_at is None:
            payload["started_at"] = claimed_at

        try:
            query = (
                self.supabase.table(self.config.jobs_table)
                .update(payload)
                .eq("request_id", candidate.request_id)
                .eq("status", candidate.status)
            )
            if candidate.locked_at:
                query = query.eq("locked_at", candidate.locked_at)
            response = query.execute()
        except Exception as exc:
            raise RetryableWorkerError(f"Supabase fallback job claim update failed: {exc}") from exc

        row = _first_row(getattr(response, "data", None))
        if row is None:
            return None
        return normalize_job_row(row)

    def get_result_by_request_id(self, request_id: str) -> JudgeApiResultRecord | None:
        try:
            response = (
                self.supabase.table(self.config.results_table)
                .select("*")
                .eq("request_id", request_id)
                .limit(1)
                .execute()
            )
        except Exception as exc:
            raise RetryableWorkerError(f"Supabase result lookup failed: {exc}") from exc

        row = _first_row(getattr(response, "data", None))
        if row is None:
            return None
        return normalize_result_row(row)

    def get_next_pending_retry_at(self) -> datetime | None:
        now_iso = utc_now_iso()
        try:
            response = (
                self.supabase.table(self.config.jobs_table)
                .select("next_attempt_at")
                .eq("status", JUDGE_JOB_STATUS_PENDING)
                .gt("next_attempt_at", now_iso)
                .order("next_attempt_at")
                .limit(1)
                .execute()
            )
        except Exception as exc:
            raise RetryableWorkerError(f"Supabase retry schedule lookup failed: {exc}") from exc

        row = _first_row(getattr(response, "data", None))
        if row is None:
            return None
        return _as_optional_datetime(row.get("next_attempt_at"))

    def download_input_bytes(self, job: JudgeApiJob) -> bytes:
        bucket = job.input_bucket or self.config.input_bucket
        try:
            response = self.supabase.storage.from_(bucket).download(job.input_storage_path)
        except Exception as exc:
            raise RetryableWorkerError(f"Supabase storage download failed: {exc}") from exc
        data = _coerce_download_bytes(response)
        if not data:
            raise RetryableWorkerError("Supabase storage download returned an empty object.")
        return data

    def archive_input_image(self, job: JudgeApiJob, raw_bytes: bytes) -> ArchivedJudgeImage:
        archive_path = self._archive_path_for_job(job)
        try:
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            archive_path.write_bytes(raw_bytes)
        except OSError as exc:
            raise RetryableWorkerError(f"Local judged image archive write failed: {exc}") from exc
        return ArchivedJudgeImage(local_path=archive_path, filename=archive_path.name)

    def ensure_archived_input_image(self, job: JudgeApiJob) -> ArchivedJudgeImage:
        archive_path = self._archive_path_for_job(job)
        if archive_path.exists():
            return ArchivedJudgeImage(local_path=archive_path, filename=archive_path.name)
        raw_bytes = self.download_input_bytes(job)
        return self.archive_input_image(job, raw_bytes)

    def delete_input_object(self, job: JudgeApiJob) -> None:
        bucket = job.input_bucket or self.config.input_bucket
        try:
            self.supabase.storage.from_(bucket).remove([job.input_storage_path])
        except Exception as exc:
            raise RetryableWorkerError(f"Supabase storage delete failed: {exc}") from exc

    def mark_job_processing(
        self,
        job: JudgeApiJob,
        *,
        piecerate_job_id: str,
        piecerate_request_id: str | None,
    ) -> None:
        self._update_job(
            job.request_id,
            {
                "status": JUDGE_JOB_STATUS_PROCESSING,
                "piecerate_job_id": piecerate_job_id,
                "piecerate_request_id": piecerate_request_id,
                "locked_at": utc_now_iso(),
                "locked_by": self.config.worker_id,
                "updated_at": utc_now_iso(),
            },
        )

    def refresh_job_lock(
        self,
        job: JudgeApiJob,
        *,
        status: str,
        piecerate_job_id: str | None,
        piecerate_request_id: str | None,
    ) -> None:
        self._update_job(
            job.request_id,
            {
                "status": status,
                "piecerate_job_id": piecerate_job_id,
                "piecerate_request_id": piecerate_request_id,
                "locked_at": utc_now_iso(),
                "locked_by": self.config.worker_id,
                "updated_at": utc_now_iso(),
            },
        )

    def mark_job_completed(
        self,
        job: JudgeApiJob,
        *,
        piecerate_job_id: str | None,
        piecerate_request_id: str | None,
    ) -> None:
        self._update_job(
            job.request_id,
            {
                "status": JUDGE_JOB_STATUS_COMPLETED,
                "piecerate_job_id": piecerate_job_id,
                "piecerate_request_id": piecerate_request_id,
                "locked_at": None,
                "locked_by": None,
                "last_error": None,
                "completed_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            },
        )

    def mark_job_failed(
        self,
        job: JudgeApiJob,
        *,
        last_error: str,
        piecerate_job_id: str | None,
        piecerate_request_id: str | None,
    ) -> None:
        self._update_job(
            job.request_id,
            {
                "status": JUDGE_JOB_STATUS_FAILED,
                "piecerate_job_id": piecerate_job_id,
                "piecerate_request_id": piecerate_request_id,
                "locked_at": None,
                "locked_by": None,
                "last_error": last_error,
                "completed_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            },
        )

    def finalize_job_from_existing_result(self, job: JudgeApiJob, existing: JudgeApiResultRecord) -> None:
        if existing.terminal_status == JUDGE_JOB_STATUS_COMPLETED:
            self.mark_job_completed(
                job,
                piecerate_job_id=existing.judge_api_job_id,
                piecerate_request_id=existing.judge_api_request_id,
            )
            return
        self.mark_job_failed(
            job,
            last_error=extract_error_message(existing.error_payload),
            piecerate_job_id=existing.judge_api_job_id,
            piecerate_request_id=existing.judge_api_request_id,
        )

    def requeue_job(self, job: JudgeApiJob, *, last_error: str) -> None:
        retry_after_seconds = self.config.retry_delay_seconds_for_attempt(job.worker_attempt_count)
        self._update_job(
            job.request_id,
            {
                "status": JUDGE_JOB_STATUS_PENDING,
                "locked_at": None,
                "locked_by": None,
                "last_error": last_error,
                "next_attempt_at": utc_in_seconds_iso(retry_after_seconds),
                "updated_at": utc_now_iso(),
            },
        )

    def upsert_result(self, payload: dict[str, Any]) -> None:
        try:
            (
                self.supabase.table(self.config.results_table)
                .upsert(payload, on_conflict="request_id")
                .execute()
            )
        except Exception as exc:
            raise RetryableWorkerError(f"Supabase result upsert failed: {exc}") from exc

    def _update_job(self, request_id: str, payload: dict[str, Any]) -> None:
        try:
            (
                self.supabase.table(self.config.jobs_table)
                .update(payload)
                .eq("request_id", request_id)
                .execute()
            )
        except Exception as exc:
            raise RetryableWorkerError(f"Supabase job update failed: {exc}") from exc

    def _archive_path_for_job(self, job: JudgeApiJob) -> Path:
        archive_name = _safe_archive_filename(job)
        return self.config.judged_image_archive_dir / job.request_id / archive_name


class PiecerateClient:
    def __init__(
        self,
        config: JudgeApiHandoffConfig | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config or JudgeApiHandoffConfig.from_env()
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=self.config.judge_api_timeout_ms / 1000.0,
            headers={
                "Authorization": f"Bearer {self.config.judge_api_token}",
                "User-Agent": DEFAULT_USER_AGENT,
            },
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def submit_prediction(self, raw_bytes: bytes, *, filename: str | None) -> PiecerateSubmission:
        payload = {
            "image_b64": base64.b64encode(raw_bytes).decode("ascii"),
            "filename": filename,
            "include_debug": False,
        }
        response = self._request("POST", "/predictions", json=payload)
        body = _response_json(response, retryable_on_invalid_json=True)

        if response.status_code == 202:
            job_id = _as_optional_str(body.get("job_id"))
            if not job_id:
                raise RetryableWorkerError(
                    "Piecerate accepted the request without returning a job_id.",
                    http_status=response.status_code,
                    payload=body,
                )
            return PiecerateSubmission(
                job_id=job_id,
                request_id=_as_optional_str(body.get("request_id")),
                http_status=response.status_code,
                payload=body,
            )

        self._raise_for_unexpected_prediction_response(response.status_code, body)
        raise AssertionError("unreachable")

    def get_prediction_status(self, piecerate_job_id: str) -> PiecerateStatusResponse:
        response = self._request(
            "GET",
            f"/predictions/{piecerate_job_id}",
            params={"wait_ms": self.config.poll_wait_ms},
        )
        body = _response_json(response, retryable_on_invalid_json=True)

        if response.status_code == 200:
            status_value = _as_optional_str(body.get("status"))
            if status_value in PIECERATE_NON_TERMINAL_STATUSES | {PIECERATE_COMPLETED, PIECERATE_FAILED}:
                return PiecerateStatusResponse(
                    job_id=_as_optional_str(body.get("job_id")) or piecerate_job_id,
                    request_id=_as_optional_str(body.get("request_id")),
                    status=status_value,
                    http_status=response.status_code,
                    payload=body,
                )
            raise RetryableWorkerError(
                "Piecerate returned an unknown job status.",
                http_status=response.status_code,
                payload=body,
            )

        if response.status_code in {401, 403}:
            raise TerminalJudgeApiError(
                extract_error_message(body),
                http_status=response.status_code,
                payload=body,
                piecerate_job_id=piecerate_job_id,
            )
        if response.status_code in {404, 429} or response.status_code >= 500:
            raise RetryableWorkerError(
                extract_error_message(body),
                http_status=response.status_code,
                payload=body,
            )
        raise TerminalJudgeApiError(
            extract_error_message(body),
            http_status=response.status_code,
            payload=body,
            piecerate_job_id=piecerate_job_id,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self.client.request(method, f"{self.config.judge_api_base_url}{path}", **kwargs)
        except httpx.TimeoutException as exc:
            raise RetryableWorkerError("Piecerate request timed out.") from exc
        except httpx.RequestError as exc:
            raise RetryableWorkerError(f"Piecerate request failed: {exc}") from exc

    def _raise_for_unexpected_prediction_response(self, http_status: int, payload: Any) -> None:
        message = extract_error_message(payload)
        if http_status in {401, 403, 400, 404, 422}:
            raise TerminalJudgeApiError(message, http_status=http_status, payload=payload)
        if http_status == 429 or http_status >= 500:
            raise RetryableWorkerError(message, http_status=http_status, payload=payload)
        raise TerminalJudgeApiError(message, http_status=http_status, payload=payload)


def create_supabase_client(supabase_url: str, service_role_key: str) -> Any:
    from supabase import create_client

    return create_client(supabase_url, service_role_key)


def normalize_job_row(row: dict[str, Any]) -> JudgeApiJob:
    input_storage_path = _as_optional_str(row.get("input_storage_path"))
    if not input_storage_path:
        raise RetryableWorkerError("Claimed judge_api_jobs row is missing input_storage_path.")
    return JudgeApiJob(
        request_id=_required_row_str(row, "request_id"),
        status=_required_row_str(row, "status"),
        created_at=_as_optional_str(row.get("created_at")),
        started_at=_as_optional_str(row.get("started_at")),
        input_storage_bucket=_as_optional_str(row.get("input_storage_bucket")),
        input_storage_path=input_storage_path,
        filename=_as_optional_str(row.get("filename")),
        image_mime_type=_as_optional_str(row.get("image_mime_type")),
        image_size_bytes=_as_optional_int(row.get("image_size_bytes")),
        judge_image_hash_sha256=_as_optional_str(row.get("judge_image_hash_sha256")),
        base_image_hash_sha256=_as_optional_str(row.get("base_image_hash_sha256")),
        llm_judgement_engine_id=_as_optional_str(row.get("llm_judgement_engine_id")),
        llm_model=_as_optional_str(row.get("llm_model")),
        worker_attempt_count=_as_optional_int(row.get("worker_attempt_count")) or 0,
        next_attempt_at=_as_optional_str(row.get("next_attempt_at")),
        locked_at=_as_optional_str(row.get("locked_at")),
        locked_by=_as_optional_str(row.get("locked_by")),
        piecerate_job_id=_as_optional_str(row.get("piecerate_job_id")),
        piecerate_request_id=_as_optional_str(row.get("piecerate_request_id")),
        last_error=_as_optional_str(row.get("last_error")),
        completed_at=_as_optional_str(row.get("completed_at")),
    )


def normalize_result_row(row: dict[str, Any]) -> JudgeApiResultRecord:
    return JudgeApiResultRecord(
        request_id=_required_row_str(row, "request_id"),
        status=_required_row_str(row, "status"),
        judge_api_job_id=_as_optional_str(row.get("judge_api_job_id")),
        judge_api_request_id=_as_optional_str(row.get("judge_api_request_id")),
        judge_api_model_version=_as_optional_str(row.get("judge_api_model_version")),
        judge_api_http_status=_as_optional_int(row.get("judge_api_http_status")),
        response_payload=row.get("response_payload"),
        error_payload=row.get("error_payload"),
    )


def build_result_row_from_success(
    *,
    job: JudgeApiJob,
    archived_image: ArchivedJudgeImage,
    request_id: str,
    piecerate_job_id: str,
    piecerate_request_id: str | None,
    response_payload: dict[str, Any],
    http_status: int,
) -> dict[str, Any]:
    enriched_response_payload = attach_source_image_metadata(response_payload, job, archived_image)
    result_payload = _extract_result_payload(response_payload)
    return {
        "request_id": request_id,
        "status": JUDGE_JOB_STATUS_COMPLETED,
        "llm_judgement_engine_id": job.llm_judgement_engine_id,
        "llm_model": job.llm_model,
        "judge_image_hash_sha256": job.judge_image_hash_sha256,
        "base_image_hash_sha256": job.base_image_hash_sha256,
        "image_mime_type": job.image_mime_type,
        "image_size_bytes": job.image_size_bytes,
        "filename": job.filename,
        "image_usable": result_payload.get("image_usable"),
        "medium": result_payload.get("medium"),
        "overall_score": result_payload.get("overall_score"),
        "legibility": result_payload.get("legibility"),
        "letter_structure": result_payload.get("letter_structure"),
        "line_quality": result_payload.get("line_quality"),
        "composition": result_payload.get("composition"),
        "color_harmony": result_payload.get("color_harmony"),
        "originality": result_payload.get("originality"),
        "judge_api_job_id": piecerate_job_id,
        "judge_api_request_id": piecerate_request_id or _as_optional_str(response_payload.get("request_id")),
        "judge_api_model_version": _as_optional_str(response_payload.get("model_version"))
        or _as_optional_str(result_payload.get("model_version")),
        "judge_api_http_status": http_status,
        "response_payload": enriched_response_payload,
        "error_payload": None,
    }


def build_result_row_from_error(
    *,
    job: JudgeApiJob,
    archived_image: ArchivedJudgeImage | None,
    request_id: str,
    piecerate_job_id: str | None,
    piecerate_request_id: str | None,
    error_payload: Any,
    http_status: int | None,
    last_error: str,
) -> dict[str, Any]:
    merged_error_payload = attach_error_source_image_metadata(
        error_payload,
        job,
        archived_image,
        last_error=last_error,
    )
    return {
        "request_id": request_id,
        "status": JUDGE_JOB_STATUS_FAILED,
        "llm_judgement_engine_id": job.llm_judgement_engine_id,
        "llm_model": job.llm_model,
        "judge_image_hash_sha256": job.judge_image_hash_sha256,
        "base_image_hash_sha256": job.base_image_hash_sha256,
        "image_mime_type": job.image_mime_type,
        "image_size_bytes": job.image_size_bytes,
        "filename": job.filename,
        "image_usable": None,
        "medium": None,
        "overall_score": None,
        "legibility": None,
        "letter_structure": None,
        "line_quality": None,
        "composition": None,
        "color_harmony": None,
        "originality": None,
        "judge_api_job_id": piecerate_job_id,
        "judge_api_request_id": piecerate_request_id,
        "judge_api_model_version": _as_optional_str(_read_mapping_value(error_payload, "model_version")),
        "judge_api_http_status": http_status,
        "response_payload": None,
        "error_payload": merged_error_payload,
    }


def build_worker_id() -> str:
    return socket.gethostname()


def build_source_image_reference(job: JudgeApiJob, archived_image: ArchivedJudgeImage | None) -> dict[str, Any]:
    return {
        "storage_kind": "local_disk",
        "local_path": None if archived_image is None else str(archived_image.local_path),
        "local_filename": None if archived_image is None else archived_image.filename,
        "original_filename": job.filename,
        "original_supabase_bucket": job.input_bucket,
        "original_supabase_path": job.input_storage_path,
        "mime_type": job.image_mime_type,
        "size_bytes": job.image_size_bytes,
        "judge_image_hash_sha256": job.judge_image_hash_sha256,
        "base_image_hash_sha256": job.base_image_hash_sha256,
    }


def attach_source_image_metadata(
    payload: dict[str, Any],
    job: JudgeApiJob,
    archived_image: ArchivedJudgeImage | None,
) -> dict[str, Any]:
    enriched_payload = dict(payload)
    enriched_payload["source_image"] = build_source_image_reference(job, archived_image)
    return enriched_payload


def attach_error_source_image_metadata(
    payload: Any,
    job: JudgeApiJob,
    archived_image: ArchivedJudgeImage | None,
    *,
    last_error: str,
) -> Any:
    if isinstance(payload, dict):
        enriched_payload = dict(payload)
        enriched_payload.setdefault("worker_last_error", last_error)
        enriched_payload["source_image"] = build_source_image_reference(job, archived_image)
        return enriched_payload
    return {
        "message": extract_error_message(payload),
        "worker_last_error": last_error,
        "source_image": build_source_image_reference(job, archived_image),
    }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_in_seconds_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def extract_error_message(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("message", "error", "detail", "worker_last_error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    return "Judge API processing failed."


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"Missing required environment variable {name}.")
    return value.strip()


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive number.")
    return value


def _ignored_positive_float_env(name: str, default: float) -> float:
    value = _positive_float_env(name, default)
    raw = os.environ.get(name)
    if raw is not None and raw.strip():
        logger.warning("%s is ignored in event-driven handoff mode.", name)
    return value


def _parse_backoff_schedule(raw: str | None) -> tuple[int, ...]:
    if raw is None or not raw.strip():
        return DEFAULT_BACKOFF_SCHEDULE
    values = []
    for item in raw.split(","):
        stripped = item.strip()
        if not stripped:
            continue
        numeric_value = int(stripped)
        if numeric_value <= 0:
            raise ValueError("JUDGE_JOB_BACKOFF_SCHEDULE_SECONDS values must be positive integers.")
        values.append(numeric_value)
    if not values:
        raise ValueError("JUDGE_JOB_BACKOFF_SCHEDULE_SECONDS must include at least one delay.")
    return tuple(values)


def _coerce_download_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    data = getattr(value, "data", None)
    if isinstance(data, bytes):
        return data
    raise RetryableWorkerError("Supabase storage download returned an unexpected response shape.")


def _first_row(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, list):
        if not value:
            return None
        row = value[0]
        return row if isinstance(row, dict) else None
    if isinstance(value, dict):
        return value
    return None


def _required_row_str(row: dict[str, Any], key: str) -> str:
    value = _as_optional_str(row.get(key))
    if value is None:
        raise RetryableWorkerError(f"Supabase row is missing required field {key}.")
    return value


def _as_optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _as_optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_optional_datetime(value: Any) -> datetime | None:
    raw = _as_optional_str(value)
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _response_json(response: httpx.Response, *, retryable_on_invalid_json: bool) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        if retryable_on_invalid_json:
            raise RetryableWorkerError(
                "Piecerate returned invalid JSON.",
                http_status=response.status_code,
                payload={"raw_body": response.text[:1000]},
            ) from exc
        raise
    if isinstance(payload, dict):
        return payload
    raise RetryableWorkerError(
        "Piecerate returned a non-object JSON payload.",
        http_status=response.status_code,
        payload={"payload": payload},
    )


def _extract_result_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result_payload = payload.get("result")
    if isinstance(result_payload, dict):
        return result_payload
    return payload


def _read_mapping_value(payload: Any, key: str) -> Any:
    if isinstance(payload, dict):
        return payload.get(key)
    return None


def _safe_archive_filename(job: JudgeApiJob) -> str:
    if job.filename and job.filename.strip():
        candidate = job.filename.strip().replace("\\", "_").replace("/", "_")
        return candidate[:200]
    input_name = Path(job.input_storage_path).name.strip()
    if input_name:
        return input_name.replace("\\", "_").replace("/", "_")[:200]
    return f"{job.request_id}.img"


def _is_missing_claim_rpc_error(exc: Exception) -> bool:
    message = str(exc)
    return "claim_next_judge_api_job" in message and "PGRST202" in message
