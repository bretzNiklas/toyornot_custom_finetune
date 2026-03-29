from __future__ import annotations

import logging
import signal
import threading
import time
from pathlib import Path

from deploy.local_queue import (
    JOB_STATUS_FAILED,
    JOB_STATUS_PROCESSING,
    QueueConfig,
    JobRecord,
    LocalJobQueue,
    build_worker_id,
)
from deploy.prediction_runtime import MODEL_VERSION, PredictionService, PredictionValidationError


logger = logging.getLogger("graffiti_student_worker")
logging.basicConfig(level=logging.INFO)


def process_claimed_job(
    queue: LocalJobQueue,
    predictor: PredictionService,
    job: JobRecord,
) -> None:
    started = time.perf_counter()
    payload_path = Path(job.payload_path or "")
    try:
        if not payload_path.exists():
            raise PredictionValidationError("payload_missing")
        raw = payload_path.read_bytes()
        result_payload = predictor.predict_bytes(
            raw,
            filename=job.filename,
            include_debug=job.include_debug,
        )
        result_payload["request_id"] = job.request_id
        result_payload["model_version"] = MODEL_VERSION
        queue.complete_job(
            job_id=job.job_id,
            result_payload=result_payload,
            processing_duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        payload_path.unlink(missing_ok=True)
        logger.info("Completed queued job %s.", job.job_id)
    except PredictionValidationError as exc:
        queue.fail_job(
            job_id=job.job_id,
            error_code=exc.code,
            error_message=exc.message,
            processing_duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        payload_path.unlink(missing_ok=True)
        logger.warning("Queued job %s failed permanently: %s", job.job_id, exc.code)
    except Exception as exc:
        processing_duration_ms = round((time.perf_counter() - started) * 1000, 2)
        if job.attempt_count >= queue.config.total_allowed_attempts:
            queue.fail_job(
                job_id=job.job_id,
                error_code="internal_error",
                error_message=str(exc),
                processing_duration_ms=processing_duration_ms,
            )
            payload_path.unlink(missing_ok=True)
            logger.exception("Queued job %s exhausted retries.", job.job_id)
            return
        queue.requeue_job(
            job_id=job.job_id,
            error_code="internal_error",
            error_message=str(exc),
        )
        logger.exception("Queued job %s failed transiently and was requeued.", job.job_id)


def worker_loop(config: QueueConfig, worker_id: str, stop_event: threading.Event) -> None:
    queue = LocalJobQueue(config)
    predictor = PredictionService()
    last_cleanup = 0.0
    last_heartbeat = 0.0
    queue.heartbeat_worker(worker_id, current_job_id=None, status="starting")
    try:
        while not stop_event.is_set():
            now = time.time()
            if now - last_heartbeat >= config.worker_heartbeat_interval_seconds:
                queue.heartbeat_worker(worker_id, current_job_id=None, status="idle")
                last_heartbeat = now
            if now - last_cleanup >= max(config.worker_heartbeat_interval_seconds, 30):
                queue.cleanup()
                last_cleanup = now

            job = queue.claim_next_job(worker_id)
            if job is None:
                stop_event.wait(config.worker_idle_poll_seconds)
                continue

            queue.heartbeat_worker(worker_id, current_job_id=job.job_id, status=JOB_STATUS_PROCESSING)
            process_claimed_job(queue, predictor, job)
            queue.heartbeat_worker(worker_id, current_job_id=None, status="idle")
            last_heartbeat = time.time()
    finally:
        queue.delete_worker(worker_id)


def run_worker_service(config: QueueConfig | None = None) -> None:
    worker_config = config or QueueConfig.from_env()
    queue = LocalJobQueue(worker_config)
    queue.initialize()
    queue.cleanup()

    stop_event = threading.Event()
    threads: list[threading.Thread] = []
    for index in range(worker_config.worker_concurrency):
        worker_id = build_worker_id(index)
        thread = threading.Thread(
            target=worker_loop,
            args=(worker_config, worker_id, stop_event),
            name=f"queue-worker-{index}",
            daemon=True,
        )
        threads.append(thread)
        thread.start()

    def handle_signal(signum, frame) -> None:  # type: ignore[no-untyped-def]
        logger.info("Received signal %s, stopping worker service.", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=5)


def main() -> None:
    run_worker_service()


if __name__ == "__main__":
    main()
