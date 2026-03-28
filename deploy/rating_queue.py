from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


logger = logging.getLogger("graffiti_rating_queue")

SUPPORTED_ENGINE_ID = "judge-api-v1"
UNRATED_IMAGE_MESSAGE = "This image could not be rated. Upload one clear photo of a physical graffiti piece."
UNRATED_MEDIA = {"digital", "other_or_unclear"}

DEFAULT_GRAFFITI_API_URL = "http://127.0.0.1:8000"
DEFAULT_QUEUE_NAME = "rating_dispatch"
DEFAULT_NOTIFY_CHANNEL = "rating_queue_wakeup"
DEFAULT_BATCH_SIZE = 25
DEFAULT_VISIBILITY_TIMEOUT_SECONDS = 300
DEFAULT_STALE_AFTER_SECONDS = 300
DEFAULT_IDLE_RECONCILE_SECONDS = 300
DEFAULT_MAX_RETRIES = 3
DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0


class RatingQueueError(Exception):
    """Base class for queue worker errors."""


class ConfigurationError(RatingQueueError):
    """Raised when the worker environment is incomplete."""


class DatabaseError(RatingQueueError):
    """Raised when the database queue operations fail."""


class InvalidQueueMessageError(RatingQueueError):
    """Raised when a queue message payload cannot be parsed."""


class InvalidJobPayloadError(RatingQueueError):
    """Raised when a queued job row cannot be normalized."""


class JudgeApiError(RatingQueueError):
    """Raised when the local Judge API returned a permanent error."""


class RetryableJudgeApiError(RatingQueueError):
    """Raised when the local Judge API failure should be retried."""


class UnratedImageError(JudgeApiError):
    """Raised when the Judge API payload is not scorable."""


@dataclass(frozen=True)
class QueueWorkerConfig:
    supabase_db_url: str
    queue_name: str
    notify_channel: str
    batch_size: int
    visibility_timeout_seconds: int
    stale_after_seconds: int
    idle_reconcile_seconds: int
    max_retries: int
    graffiti_api_url: str
    graffiti_api_token: str
    http_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> "QueueWorkerConfig":
        supabase_db_url = _optional_env("SUPABASE_SESSION_POOLER_URL") or _required_env("SUPABASE_DB_URL")
        graffiti_api_token = _optional_env("GRAFFITI_API_TOKEN") or _required_env("AUTH_TOKEN")
        return cls(
            supabase_db_url=supabase_db_url,
            queue_name=_optional_env("RATING_QUEUE_NAME") or DEFAULT_QUEUE_NAME,
            notify_channel=_optional_env("RATING_QUEUE_NOTIFY_CHANNEL") or DEFAULT_NOTIFY_CHANNEL,
            batch_size=_positive_int_env("RATING_QUEUE_BATCH_SIZE", DEFAULT_BATCH_SIZE),
            visibility_timeout_seconds=_positive_int_env(
                "RATING_QUEUE_VISIBILITY_TIMEOUT_SECONDS",
                DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
            ),
            stale_after_seconds=_positive_int_env(
                "RATING_QUEUE_STALE_AFTER_SECONDS",
                DEFAULT_STALE_AFTER_SECONDS,
            ),
            idle_reconcile_seconds=_positive_int_env(
                "RATING_QUEUE_IDLE_RECONCILE_SECONDS",
                DEFAULT_IDLE_RECONCILE_SECONDS,
            ),
            max_retries=_positive_int_env("RATING_QUEUE_MAX_RETRIES", DEFAULT_MAX_RETRIES),
            graffiti_api_url=(_optional_env("GRAFFITI_API_URL") or DEFAULT_GRAFFITI_API_URL).rstrip("/"),
            graffiti_api_token=graffiti_api_token,
        )


@dataclass(frozen=True)
class RatingJobPayload:
    image_data_url: str
    image_file_name: str | None
    image_hash_sha256: str
    image_mime_type: str
    image_size_bytes: int
    requested_critique_language: str
    requested_judgement_engine_id: str
    requested_judgement_model: str | None


@dataclass(frozen=True)
class RatingJob:
    id: str
    request_id: str
    status: str
    payload: RatingJobPayload
    judgement_engine_id: str


@dataclass(frozen=True)
class QueueMessage:
    msg_id: int
    read_count: int
    job_id: str
    request_id: str


@dataclass(frozen=True)
class RatingJobState:
    id: str
    status: str


def _optional_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _required_env(name: str) -> str:
    value = _optional_env(name)
    if value is None:
        raise ConfigurationError(f"Missing required environment variable {name}.")
    return value


def _positive_int_env(name: str, default: int) -> int:
    raw_value = _optional_env(name)
    if raw_value is None:
        return default

    try:
        numeric_value = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"Environment variable {name} must be a positive integer.") from exc

    if numeric_value <= 0:
        raise ConfigurationError(f"Environment variable {name} must be a positive integer.")
    return numeric_value


def _parse_optional_string(value: Any) -> str | None:
    if isinstance(value, UUID):
        return str(value)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _read_record_value(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record:
            return record[key]
    return None


def _parse_positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        numeric_value = int(value)
    except (TypeError, ValueError):
        return None
    return numeric_value if numeric_value > 0 else None


def _parse_optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _parse_request_payload(value: Any) -> RatingJobPayload:
    if not isinstance(value, dict):
        raise InvalidJobPayloadError("Queued rating job request_payload must be a JSON object.")

    image_data_url = _parse_optional_string(_read_record_value(value, "imageDataUrl", "image_data_url"))
    image_file_name = _parse_optional_string(_read_record_value(value, "imageFileName", "image_file_name"))
    image_hash_sha256 = _parse_optional_string(_read_record_value(value, "imageHashSha256", "image_hash_sha256"))
    image_mime_type = _parse_optional_string(_read_record_value(value, "imageMimeType", "image_mime_type"))
    image_size_bytes = _parse_positive_int(_read_record_value(value, "imageSizeBytes", "image_size_bytes"))
    requested_critique_language = _parse_optional_string(
        _read_record_value(value, "requestedCritiqueLanguage", "requested_critique_language"),
    )
    requested_judgement_engine_id = _parse_optional_string(
        _read_record_value(value, "requestedJudgementEngineId", "requested_judgement_engine_id"),
    )
    requested_judgement_model = _parse_optional_string(
        _read_record_value(value, "requestedJudgementModel", "requested_judgement_model"),
    )

    if (
        not image_data_url
        or not image_hash_sha256
        or not image_mime_type
        or image_size_bytes is None
        or not requested_critique_language
        or not requested_judgement_engine_id
    ):
        raise InvalidJobPayloadError("Queued rating job request_payload is missing required fields.")

    return RatingJobPayload(
        image_data_url=image_data_url,
        image_file_name=image_file_name,
        image_hash_sha256=image_hash_sha256,
        image_mime_type=image_mime_type,
        image_size_bytes=image_size_bytes,
        requested_critique_language=requested_critique_language,
        requested_judgement_engine_id=requested_judgement_engine_id,
        requested_judgement_model=requested_judgement_model,
    )


def normalize_rating_job(row: Any) -> RatingJob:
    if not isinstance(row, dict):
        raise InvalidJobPayloadError("Queued rating job row must be a JSON object.")

    job_id = _parse_optional_string(row.get("id"))
    request_id = _parse_optional_string(_read_record_value(row, "request_id", "requestId")) or ""
    status = _parse_optional_string(row.get("status")) or "queued"
    payload = _parse_request_payload(_read_record_value(row, "request_payload", "requestPayload"))
    judgement_engine_id = (
        _parse_optional_string(_read_record_value(row, "judgement_engine_id", "judgementEngineId"))
        or payload.requested_judgement_engine_id
    )

    if not job_id or not judgement_engine_id:
        raise InvalidJobPayloadError("Queued rating job row is missing required fields.")

    return RatingJob(
        id=job_id,
        request_id=request_id,
        status=status,
        payload=payload,
        judgement_engine_id=judgement_engine_id,
    )


def normalize_queue_message(row: Any) -> QueueMessage:
    if not isinstance(row, dict):
        raise InvalidQueueMessageError("Queue message row must be a JSON object.")

    msg_id = row.get("msg_id")
    read_count = row.get("read_ct")
    message_payload = row.get("message")
    if not isinstance(msg_id, int) or not isinstance(read_count, int):
        raise InvalidQueueMessageError("Queue message row is missing msg_id or read_ct.")
    if not isinstance(message_payload, dict):
        raise InvalidQueueMessageError("Queue message payload must be a JSON object.")

    job_id = _parse_optional_string(_read_record_value(message_payload, "jobId", "job_id"))
    request_id = _parse_optional_string(_read_record_value(message_payload, "requestId", "request_id")) or ""
    if not job_id:
        raise InvalidQueueMessageError("Queue message payload is missing jobId.")

    return QueueMessage(
        msg_id=msg_id,
        read_count=read_count,
        job_id=job_id,
        request_id=request_id,
    )


def normalize_rating_job_state(row: Any) -> RatingJobState | None:
    if not isinstance(row, dict):
        return None

    job_id = _parse_optional_string(row.get("id"))
    status = _parse_optional_string(row.get("status"))
    if not job_id or status not in {"queued", "processing", "completed", "failed"}:
        return None
    return RatingJobState(id=job_id, status=status)


def extract_base64_payload(image_data_url: str) -> str:
    trimmed = image_data_url.strip()
    if not trimmed:
        raise InvalidJobPayloadError("Queued rating job imageDataUrl is empty.")

    prefix, separator, payload = trimmed.partition(",")
    if separator and prefix.lower().startswith("data:") and ";base64" in prefix.lower():
        normalized_payload = payload.strip()
        if not normalized_payload:
            raise InvalidJobPayloadError("Queued rating job imageDataUrl is missing its base64 payload.")
        return normalized_payload
    return trimmed


def _to_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return None
    return numeric_value if numeric_value == numeric_value and numeric_value not in (float("inf"), float("-inf")) else None


def _normalize_to_ten_point_scale(score: float) -> float:
    return score / 10.0 if score > 10 else score


def _clamp_category_score(score: float) -> int:
    normalized_score = _normalize_to_ten_point_scale(score)
    return max(0, min(10, round(normalized_score)))


def _clamp_total_score(score: float) -> float:
    normalized_score = _normalize_to_ten_point_scale(score)
    clamped_score = max(0.0, min(10.0, normalized_score))
    return round(clamped_score * 10) / 10


def _parse_category_score(value: Any) -> int:
    numeric_value = _to_number(value)
    if numeric_value is None:
        raise UnratedImageError(UNRATED_IMAGE_MESSAGE)
    return _clamp_category_score(numeric_value)


def _parse_total_score(value: Any) -> float:
    numeric_value = _to_number(value)
    if numeric_value is None:
        raise UnratedImageError(UNRATED_IMAGE_MESSAGE)
    return _clamp_total_score(numeric_value)


def _require_result_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidJobPayloadError("Queued rating result payload must be a JSON object.")
    return value


def _read_required_result_category_scores(value: Any) -> dict[str, int]:
    record = _require_result_record(value)
    category_scores = {
        "legibility": _parse_category_score(_read_record_value(record, "legibility")),
        "letterStructure": _parse_category_score(_read_record_value(record, "letterStructure", "letter_structure")),
        "lineQuality": _parse_category_score(_read_record_value(record, "lineQuality", "line_quality")),
        "composition": _parse_category_score(_read_record_value(record, "composition")),
        "colorHarmony": _parse_category_score(_read_record_value(record, "colorHarmony", "color_harmony")),
        "originality": _parse_category_score(_read_record_value(record, "originality")),
    }
    return category_scores


def _derive_legacy_persistence_scores(category_scores: dict[str, int]) -> dict[str, float]:
    return {
        "lettering_style": (category_scores["legibility"] + category_scores["letterStructure"]) / 2,
        "color": category_scores["colorHarmony"],
        "composition": category_scores["composition"],
        "originality_concept": category_scores["originality"],
        "technique_complexity": (category_scores["lineQuality"] + category_scores["letterStructure"]) / 2,
    }


def build_score_log_payload(job: RatingJob, result_payload: Any) -> dict[str, Any]:
    result_record = _require_result_record(result_payload)
    category_scores = _read_required_result_category_scores(
        _read_record_value(result_record, "categoryScores", "category_scores"),
    )
    legacy_scores = _derive_legacy_persistence_scores(category_scores)
    critique = _read_record_value(result_record, "critique")

    score_log_payload: dict[str, Any] = {
        "total_score": _parse_total_score(_read_record_value(result_record, "totalScore", "total_score")),
        "lettering_style": legacy_scores["lettering_style"],
        "color": legacy_scores["color"],
        "composition": legacy_scores["composition"],
        "originality_concept": legacy_scores["originality_concept"],
        "technique_complexity": legacy_scores["technique_complexity"],
        "category_scores": category_scores,
        "critique": critique if isinstance(critique, str) else "",
        "used_fallback_critique": _parse_optional_bool(
            _read_record_value(result_record, "usedFallbackCritique", "used_fallback_critique"),
        ) is True,
        "critique_language": job.payload.requested_critique_language,
        "judgement_engine_id": (
            _parse_optional_string(_read_record_value(result_record, "judgementEngineId", "judgement_engine_id"))
            or job.judgement_engine_id
        ),
        "rating_schema_version": (
            _parse_optional_string(_read_record_value(result_record, "ratingSchemaVersion", "rating_schema_version"))
            or "v2"
        ),
        "image_mime_type": job.payload.image_mime_type,
        "image_size_bytes": job.payload.image_size_bytes,
        "model": job.payload.requested_judgement_model or job.judgement_engine_id,
        "request_id": job.request_id,
    }

    uncertainty = _to_number(_read_record_value(result_record, "uncertainty"))
    if uncertainty is not None:
        score_log_payload["uncertainty"] = max(0.0, min(1.0, round(uncertainty, 2)))

    evidence = _read_record_value(result_record, "evidence")
    if isinstance(evidence, dict):
        score_log_payload["evidence"] = evidence

    image_adequacy = _read_record_value(result_record, "imageAdequacy", "image_adequacy")
    if isinstance(image_adequacy, dict):
        score_log_payload["image_adequacy"] = image_adequacy

    share_card_verdict = _parse_optional_string(
        _read_record_value(result_record, "shareCardVerdict", "share_card_verdict"),
    )
    if share_card_verdict:
        score_log_payload["share_card_verdict"] = share_card_verdict

    return score_log_payload


def normalize_judge_api_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise JudgeApiError("Judge API returned invalid JSON.")

    if payload.get("image_usable") is not True:
        raise UnratedImageError(UNRATED_IMAGE_MESSAGE)

    medium = _parse_optional_string(payload.get("medium"))
    if (medium or "").lower() in UNRATED_MEDIA:
        raise UnratedImageError(UNRATED_IMAGE_MESSAGE)

    result = {
        "totalScore": _parse_total_score(payload.get("overall_score")),
        "categoryScores": {
            "legibility": _parse_category_score(payload.get("legibility")),
            "letterStructure": _parse_category_score(payload.get("letter_structure")),
            "lineQuality": _parse_category_score(payload.get("line_quality")),
            "composition": _parse_category_score(payload.get("composition")),
            "colorHarmony": _parse_category_score(payload.get("color_harmony")),
            "originality": _parse_category_score(payload.get("originality")),
        },
        "critique": "",
        "ratingSchemaVersion": "v2",
        "judgementEngineId": SUPPORTED_ENGINE_ID,
    }

    request_id = _parse_optional_string(payload.get("request_id"))
    if request_id:
        result["requestId"] = request_id
    return result


def build_retry_delay_seconds(read_count: int) -> int:
    return min(60, max(5, read_count * 5))


class PostgresRatingQueueClient:
    def __init__(self, config: QueueWorkerConfig, connection: psycopg.Connection[Any] | None = None) -> None:
        self.config = config
        self._owns_connection = connection is None
        self.connection = connection or psycopg.connect(
            config.supabase_db_url,
            autocommit=True,
            row_factory=dict_row,
        )

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()

    def read_visible_messages(self, *, batch_size: int | None = None) -> list[QueueMessage]:
        rows = self._fetchall(
            "select * from pgmq.read(%s, %s, %s)",
            (self.config.queue_name, self.config.visibility_timeout_seconds, batch_size or self.config.batch_size),
            "Queue read",
        )
        messages: list[QueueMessage] = []
        for row in rows:
            try:
                messages.append(normalize_queue_message(row))
            except InvalidQueueMessageError as exc:
                msg_id = row.get("msg_id") if isinstance(row, dict) else None
                if isinstance(msg_id, int):
                    logger.warning("Archiving invalid queue message %s: %s", msg_id, exc)
                    self.archive_message(msg_id)
                    continue
                raise
        return messages

    def archive_message(self, msg_id: int) -> None:
        row = self._fetchone(
            "select pgmq.archive(%s, %s) as archived",
            (self.config.queue_name, msg_id),
            "Queue archive",
        )
        if not row or row.get("archived") is not True:
            raise DatabaseError(f"Queue archive failed for message {msg_id}.")

    def set_message_visibility_timeout(self, msg_id: int, vt_offset_seconds: int) -> None:
        row = self._fetchone(
            "select * from pgmq.set_vt(%s, %s, %s)",
            (self.config.queue_name, msg_id, vt_offset_seconds),
            "Queue visibility update",
        )
        if row is None:
            raise DatabaseError(f"Queue visibility update failed for message {msg_id}.")

    def claim_rating_job(self, job_id: str) -> RatingJob | None:
        rows = self._fetchall(
            "select * from public.claim_rating_job(%s::uuid, %s)",
            (job_id, self.config.stale_after_seconds),
            "Rating job claim",
        )
        if not rows:
            return None
        return normalize_rating_job(rows[0])

    def load_job_state(self, job_id: str) -> RatingJobState | None:
        row = self._fetchone(
            "select id, status from public.rating_jobs where id = %s::uuid",
            (job_id,),
            "Rating job lookup",
        )
        return normalize_rating_job_state(row)

    def complete_rating_job(
        self,
        job_id: str,
        result_payload: dict[str, Any],
        score_log_payload: dict[str, Any],
    ) -> None:
        row = self._fetchone(
            "select * from public.complete_rating_job(%s::uuid, %s, %s)",
            (job_id, Jsonb(result_payload), Jsonb(score_log_payload)),
            "Rating queue completion",
        )
        if row is None:
            raise DatabaseError(f"Rating queue completion failed for job {job_id}.")

    def mark_job_failed(self, job_id: str, error_message: str) -> None:
        row = self._fetchone(
            """
            update public.rating_jobs
            set
              completed_at = now(),
              error_message = %s,
              status = 'failed'
            where id = %s::uuid
            returning id
            """,
            (error_message, job_id),
            "Rating queue failure update",
        )
        if row is None:
            raise DatabaseError(f"Rating queue failure update failed for job {job_id}.")

    def mark_job_retryable(self, job_id: str) -> None:
        row = self._fetchone(
            """
            update public.rating_jobs
            set
              completed_at = null,
              error_message = null,
              started_at = null,
              status = 'queued'
            where id = %s::uuid
            returning id
            """,
            (job_id,),
            "Rating queue retry reset",
        )
        if row is None:
            raise DatabaseError(f"Rating queue retry reset failed for job {job_id}.")

    def _fetchall(self, query: str, params: tuple[Any, ...], label: str) -> list[dict[str, Any]]:
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
        except psycopg.Error as exc:
            raise DatabaseError(f"{label} failed: {exc}") from exc

    def _fetchone(self, query: str, params: tuple[Any, ...], label: str) -> dict[str, Any] | None:
        rows = self._fetchall(query, params, label)
        return rows[0] if rows else None


class RatingQueueListener:
    def __init__(self, config: QueueWorkerConfig, connection: psycopg.Connection[Any] | None = None) -> None:
        self.config = config
        self._owns_connection = connection is None
        self.connection = connection or psycopg.connect(config.supabase_db_url, autocommit=True)
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(sql.SQL("LISTEN {}").format(sql.Identifier(config.notify_channel)))
        except psycopg.Error as exc:
            raise DatabaseError(f"Queue listener setup failed: {exc}") from exc

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()

    def wait_for_notification(self, timeout_seconds: int) -> str | None:
        try:
            for notification in self.connection.notifies(timeout=timeout_seconds, stop_after=1):
                payload = notification.payload.strip()
                return payload or None
            return None
        except psycopg.Error as exc:
            raise DatabaseError(f"Queue notification wait failed: {exc}") from exc


class JudgeApiClient:
    def __init__(self, config: QueueWorkerConfig, client: httpx.Client | None = None) -> None:
        self.config = config
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=config.http_timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def process_rating_job(self, job: RatingJob) -> dict[str, Any]:
        if job.judgement_engine_id != SUPPORTED_ENGINE_ID:
            raise JudgeApiError(
                f"Unsupported requestedJudgementEngineId '{job.judgement_engine_id}'. Only '{SUPPORTED_ENGINE_ID}' is supported.",
            )

        try:
            response = self.client.post(
                f"{self.config.graffiti_api_url}/predict",
                headers={"authorization": f"Bearer {self.config.graffiti_api_token}"},
                json={
                    "filename": job.payload.image_file_name,
                    "image_b64": extract_base64_payload(job.payload.image_data_url),
                    "include_debug": False,
                },
            )
        except httpx.TimeoutException as exc:
            raise RetryableJudgeApiError("Judge API request timed out.") from exc
        except httpx.RequestError as exc:
            raise RetryableJudgeApiError(f"Judge API request failed: {exc}") from exc

        if 500 <= response.status_code < 600:
            raise RetryableJudgeApiError(_extract_error_message(response))
        if not response.is_success:
            raise JudgeApiError(_extract_error_message(response))

        try:
            payload = response.json()
        except ValueError as exc:
            raise JudgeApiError("Judge API returned invalid JSON.") from exc

        return normalize_judge_api_result(payload)


def _extract_error_message(response: httpx.Response) -> str:
    body_text = response.text
    try:
        payload = response.json()
    except ValueError as exc:
        raise JudgeApiError("Judge API returned invalid JSON.") from exc

    if not isinstance(payload, dict):
        return "Judge API request failed unexpectedly."

    message = _parse_optional_string(payload.get("message")) or _parse_optional_string(payload.get("error"))
    if response.status_code == 422:
        return UNRATED_IMAGE_MESSAGE
    if message:
        return message

    logger.warning("Judge API returned an unstructured error payload: %s", body_text[:500])
    return "Judge API request failed unexpectedly."
