from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from deploy.local_queue import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PROCESSING,
    JOB_STATUS_QUEUED,
    JobRecord,
    LocalJobQueue,
)
from deploy.prediction_runtime import MODEL_VERSION, PredictionService, PredictionValidationError, decode_and_validate_image


APP_NAME = "graffiti-student-local"

logger = logging.getLogger("graffiti_student_local")
logging.basicConfig(level=logging.INFO)
auth_scheme = HTTPBearer(auto_error=False)
app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)
queue: LocalJobQueue | None = None
prediction_service: PredictionService | None = None


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


def get_queue() -> LocalJobQueue:
    global queue
    if queue is None:
        queue = LocalJobQueue()
        queue.initialize()
        queue.cleanup()
    return queue


def get_prediction_service() -> PredictionService:
    global prediction_service
    if prediction_service is None:
        prediction_service = PredictionService()
    return prediction_service


def error_response(
    code: str,
    message: str,
    status_code: int,
    request_id: str,
    *,
    extra: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload: dict[str, object] = {
        "error": code,
        "message": message,
        "request_id": request_id,
        "model_version": MODEL_VERSION,
    }
    if extra:
        payload.update(extra)
    return JSONResponse(status_code=status_code, content=payload, headers=headers or {})


def build_status_payload(job: JobRecord, queue_store: LocalJobQueue) -> dict[str, object]:
    payload: dict[str, object] = {
        "job_id": job.job_id,
        "request_id": job.request_id,
        "status": job.status,
        "model_version": MODEL_VERSION,
    }
    if job.status == JOB_STATUS_COMPLETED:
        payload["result"] = job.result_json or {}
        return payload
    if job.status == JOB_STATUS_FAILED:
        payload["error"] = job.error_code or "job_failed"
        payload["message"] = job.error_message or "Prediction job failed."
        return payload

    payload["queue_position"] = queue_store.get_job_queue_position(job)
    payload["estimated_wait_seconds"] = queue_store.estimate_job_wait_seconds(job)
    return payload


def spool_path_for_job(queue_store: LocalJobQueue, job_id: str, filename: str | None) -> Path:
    suffix = ".img"
    if filename:
        candidate = Path(filename).suffix.lower()
        if candidate and len(candidate) <= 10 and candidate.replace(".", "").isalnum():
            suffix = candidate
    return queue_store.config.spool_dir / f"{job_id}{suffix}"


@app.on_event("startup")
def initialize_runtime() -> None:
    get_queue()


@app.get("/health")
def health(_: str = Depends(authorize)) -> JSONResponse:
    queue_store = get_queue()
    snapshot = queue_store.get_health_snapshot()
    content = {
        "status": "ok" if snapshot.worker_heartbeat_fresh else "degraded",
        "model_version": MODEL_VERSION,
        "app": APP_NAME,
        "queued_jobs": snapshot.queued_jobs,
        "processing_jobs": snapshot.processing_jobs,
        "oldest_queued_age_seconds": snapshot.oldest_queued_age_seconds,
        "average_processing_seconds": round(snapshot.average_processing_seconds, 2),
        "worker_concurrency": queue_store.config.worker_concurrency,
        "fresh_worker_count": snapshot.fresh_worker_count,
        "worker_heartbeat_age_seconds": snapshot.most_recent_heartbeat_age_seconds,
        "worker_heartbeat_fresh": snapshot.worker_heartbeat_fresh,
    }
    status_code = 200 if snapshot.worker_heartbeat_fresh else 503
    return JSONResponse(status_code=status_code, content=content)


@app.post("/predict")
def predict(payload: PredictionRequest, _: str = Depends(authorize)):
    request_id = str(uuid.uuid4())
    started = time.perf_counter()
    try:
        validated = decode_and_validate_image(payload.image_b64)
        result = get_prediction_service().predict_image(
            validated.image,
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
                    "path": "/predict",
                }
            )
        )
        return result
    except PredictionValidationError as exc:
        return error_response(exc.code, exc.message, 400, request_id)
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


@app.post("/predictions")
def create_prediction_job(payload: PredictionRequest, request: Request, _: str = Depends(authorize)):
    request_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    queue_store = get_queue()
    payload_path = spool_path_for_job(queue_store, job_id, payload.filename)
    try:
        validated = decode_and_validate_image(payload.image_b64)
        payload_path.write_bytes(validated.raw_bytes)
        admission = queue_store.enqueue_job(
            job_id=job_id,
            request_id=request_id,
            filename=payload.filename,
            include_debug=payload.include_debug,
            payload_path=payload_path,
            payload_size_bytes=len(validated.raw_bytes),
        )
        if not admission.accepted:
            payload_path.unlink(missing_ok=True)
            retry_after_seconds = max(1, admission.estimated_wait_seconds)
            return error_response(
                "queue_overloaded",
                "The prediction queue is currently too busy. Retry later.",
                429,
                request_id,
                extra={
                    "job_id": job_id,
                    "queue_position": admission.queue_position,
                    "estimated_wait_seconds": admission.estimated_wait_seconds,
                },
                headers={"Retry-After": str(retry_after_seconds)},
            )
        body = {
            "job_id": admission.job_id,
            "request_id": admission.request_id,
            "status": admission.status,
            "queue_position": admission.queue_position,
            "estimated_wait_seconds": admission.estimated_wait_seconds,
            "poll_url": str(request.url_for("get_prediction_status", job_id=admission.job_id)),
            "model_version": MODEL_VERSION,
        }
        logger.info(
            json.dumps(
                {
                    "event": "prediction_job_enqueued",
                    "request_id": request_id,
                    "job_id": job_id,
                    "filename": payload.filename,
                    "queue_position": admission.queue_position,
                    "estimated_wait_seconds": admission.estimated_wait_seconds,
                }
            )
        )
        return JSONResponse(status_code=202, content=body)
    except PredictionValidationError as exc:
        payload_path.unlink(missing_ok=True)
        return error_response(exc.code, exc.message, 400, request_id)
    except HTTPException:
        raise
    except Exception:
        payload_path.unlink(missing_ok=True)
        logger.exception("Unhandled queue submission error", extra={"request_id": request_id, "job_id": job_id})
        return error_response(
            "internal_error",
            "Unexpected server error.",
            500,
            request_id,
        )


@app.get("/predictions/{job_id}", name="get_prediction_status")
def get_prediction_status(
    job_id: str,
    wait_ms: int = Query(default=0, ge=0, le=8000),
    _: str = Depends(authorize),
):
    queue_store = get_queue()
    request_id = str(uuid.uuid4())
    deadline = time.monotonic() + (wait_ms / 1000.0)
    job = queue_store.get_job(job_id)
    if job is None:
        return error_response(
            "job_not_found",
            "Prediction job was not found.",
            404,
            request_id,
        )

    while (
        wait_ms > 0
        and job.status not in {JOB_STATUS_COMPLETED, JOB_STATUS_FAILED}
        and time.monotonic() < deadline
    ):
        time.sleep(0.25)
        refreshed = queue_store.get_job(job_id)
        if refreshed is None:
            return error_response(
                "job_not_found",
                "Prediction job was not found.",
                404,
                request_id,
            )
        if refreshed.status != job.status:
            job = refreshed
            break
        job = refreshed

    return JSONResponse(status_code=200, content=build_status_payload(job, queue_store))
