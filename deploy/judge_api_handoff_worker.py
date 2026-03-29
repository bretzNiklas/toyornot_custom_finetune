from __future__ import annotations

import logging
import signal
import time

from deploy.judge_api_handoff_runtime import (
    ArchivedJudgeImage,
    JUDGE_JOB_STATUS_CLAIMED,
    JUDGE_JOB_STATUS_FAILED,
    JUDGE_JOB_STATUS_PROCESSING,
    PIECERATE_COMPLETED,
    PIECERATE_FAILED,
    PIECERATE_NON_TERMINAL_STATUSES,
    JudgeApiHandoffConfig,
    JudgeApiJob,
    PiecerateClient,
    RetryableWorkerError,
    SupabaseJudgeApiRuntime,
    TerminalJudgeApiError,
    build_result_row_from_error,
    build_result_row_from_success,
    extract_error_message,
)


logger = logging.getLogger("graffiti_judge_handoff_worker")
logging.basicConfig(level=logging.INFO)


def process_handoff_job(
    runtime: SupabaseJudgeApiRuntime,
    piecerate: PiecerateClient,
    config: JudgeApiHandoffConfig,
    job: JudgeApiJob,
) -> None:
    archived_image: ArchivedJudgeImage | None = None
    try:
        existing_result = runtime.get_result_by_request_id(job.request_id)
        if existing_result is not None and existing_result.is_terminal:
            archived_image = runtime.ensure_archived_input_image(job)
            _log_archived_input(archived_image, job)
            runtime.finalize_job_from_existing_result(job, existing_result)
            _delete_input_object_best_effort(runtime, job)
            logger.info(
                "Finalized duplicate request_id=%s from existing %s row.",
                job.request_id,
                existing_result.terminal_status,
            )
            return
        if existing_result is not None:
            logger.info(
                "Ignoring non-terminal existing result row for request_id=%s with status=%s.",
                job.request_id,
                existing_result.status,
            )

        piecerate_job_id = job.piecerate_job_id
        piecerate_request_id = job.piecerate_request_id
        if piecerate_job_id:
            logger.info(
                "Resuming stale request_id=%s with piecerate_job_id=%s.",
                job.request_id,
                piecerate_job_id,
            )
        else:
            raw_bytes = runtime.download_input_bytes(job)
            archived_image = runtime.archive_input_image(job, raw_bytes)
            _log_archived_input(archived_image, job)
            logger.info("Submitting request_id=%s to Piecerate.", job.request_id)
            submission = piecerate.submit_prediction(raw_bytes, filename=job.filename)
            piecerate_job_id = submission.job_id
            piecerate_request_id = submission.request_id
            runtime.mark_job_processing(
                job,
                piecerate_job_id=piecerate_job_id,
                piecerate_request_id=piecerate_request_id,
            )
            logger.info(
                "Piecerate accepted request_id=%s with job_id=%s.",
                job.request_id,
                piecerate_job_id,
            )
        if archived_image is None:
            archived_image = runtime.ensure_archived_input_image(job)
            _log_archived_input(archived_image, job)

        while True:
            status_response = piecerate.get_prediction_status(piecerate_job_id)
            if status_response.request_id:
                piecerate_request_id = status_response.request_id

            if status_response.status in PIECERATE_NON_TERMINAL_STATUSES:
                runtime.refresh_job_lock(
                    job,
                    status=JUDGE_JOB_STATUS_PROCESSING,
                    piecerate_job_id=piecerate_job_id,
                    piecerate_request_id=piecerate_request_id,
                )
                logger.info(
                    "Piecerate job_id=%s for request_id=%s remains %s.",
                    piecerate_job_id,
                    job.request_id,
                    status_response.status,
                )
                continue

            if status_response.status == PIECERATE_COMPLETED:
                runtime.upsert_result(
                    build_result_row_from_success(
                        job=job,
                        archived_image=archived_image,
                        request_id=job.request_id,
                        piecerate_job_id=piecerate_job_id,
                        piecerate_request_id=piecerate_request_id,
                        response_payload=status_response.payload,
                        http_status=status_response.http_status,
                    )
                )
                runtime.mark_job_completed(
                    job,
                    piecerate_job_id=piecerate_job_id,
                    piecerate_request_id=piecerate_request_id,
                )
                _delete_input_object_best_effort(runtime, job)
                logger.info(
                    "Completed request_id=%s via piecerate_job_id=%s.",
                    job.request_id,
                    piecerate_job_id,
                )
                return

            if status_response.status == PIECERATE_FAILED:
                error_message = extract_error_message(status_response.payload)
                runtime.upsert_result(
                    build_result_row_from_error(
                        job=job,
                        archived_image=archived_image,
                        request_id=job.request_id,
                        piecerate_job_id=piecerate_job_id,
                        piecerate_request_id=piecerate_request_id,
                        error_payload=status_response.payload,
                        http_status=status_response.http_status,
                        last_error=error_message,
                    )
                )
                runtime.mark_job_failed(
                    job,
                    last_error=error_message,
                    piecerate_job_id=piecerate_job_id,
                    piecerate_request_id=piecerate_request_id,
                )
                _delete_input_object_best_effort(runtime, job)
                logger.warning(
                    "Piecerate failed request_id=%s via job_id=%s: %s",
                    job.request_id,
                    piecerate_job_id,
                    error_message,
                )
                return

            raise RetryableWorkerError(
                f"Unexpected Piecerate status '{status_response.status}'.",
                http_status=status_response.http_status,
                payload=status_response.payload,
            )

    except TerminalJudgeApiError as exc:
        error_message = str(exc)
        runtime.upsert_result(
            build_result_row_from_error(
                job=job,
                archived_image=archived_image,
                request_id=job.request_id,
                piecerate_job_id=exc.piecerate_job_id or job.piecerate_job_id,
                piecerate_request_id=exc.piecerate_request_id or job.piecerate_request_id,
                error_payload=exc.payload,
                http_status=exc.http_status,
                last_error=error_message,
            )
        )
        runtime.mark_job_failed(
            job,
            last_error=error_message,
            piecerate_job_id=exc.piecerate_job_id or job.piecerate_job_id,
            piecerate_request_id=exc.piecerate_request_id or job.piecerate_request_id,
        )
        _delete_input_object_best_effort(runtime, job)
        logger.warning("Terminal Piecerate failure for request_id=%s: %s", job.request_id, error_message)
    except RetryableWorkerError as exc:
        _handle_retryable_failure(runtime, job, exc, archived_image=archived_image)
    except Exception as exc:
        _handle_retryable_failure(
            runtime,
            job,
            RetryableWorkerError(f"Worker failure: {exc}"),
            archived_image=archived_image,
        )


def run_worker_iteration(
    runtime: SupabaseJudgeApiRuntime,
    piecerate: PiecerateClient,
    config: JudgeApiHandoffConfig,
) -> bool:
    job = runtime.claim_next_job()
    if job is None:
        return False

    logger.info(
        "Claimed request_id=%s status=%s attempt=%s worker=%s.",
        job.request_id,
        job.status,
        job.worker_attempt_count,
        config.worker_id,
    )
    process_handoff_job(runtime, piecerate, config, job)
    return True


def run_worker_forever(config: JudgeApiHandoffConfig | None = None) -> None:
    worker_config = config or JudgeApiHandoffConfig.from_env()
    runtime = SupabaseJudgeApiRuntime(worker_config)
    piecerate = PiecerateClient(worker_config)
    stop_requested = False

    def handle_signal(signum, frame) -> None:  # type: ignore[no-untyped-def]
        nonlocal stop_requested
        logger.info("Received signal %s, stopping handoff worker.", signum)
        stop_requested = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        while not stop_requested:
            try:
                processed_job = run_worker_iteration(runtime, piecerate, worker_config)
            except Exception as exc:
                logger.exception("Handoff worker loop failed: %s", exc)
                time.sleep(worker_config.idle_sleep_seconds)
                continue

            if not processed_job:
                time.sleep(worker_config.idle_sleep_seconds)
    finally:
        piecerate.close()


def main() -> None:
    run_worker_forever()


def _handle_retryable_failure(
    runtime: SupabaseJudgeApiRuntime,
    job: JudgeApiJob,
    exc: RetryableWorkerError,
    archived_image: ArchivedJudgeImage | None = None,
) -> None:
    message = str(exc)
    if job.worker_attempt_count >= runtime.config.max_attempts:
        if archived_image is None:
            archived_image = _ensure_archive_best_effort(runtime, job)
        runtime.upsert_result(
            build_result_row_from_error(
                job=job,
                archived_image=archived_image,
                request_id=job.request_id,
                piecerate_job_id=job.piecerate_job_id,
                piecerate_request_id=job.piecerate_request_id,
                error_payload=exc.payload,
                http_status=exc.http_status,
                last_error=message,
            )
        )
        runtime.mark_job_failed(
            job,
            last_error=message,
            piecerate_job_id=job.piecerate_job_id,
            piecerate_request_id=job.piecerate_request_id,
        )
        _delete_input_object_best_effort(runtime, job)
        logger.exception(
            "Retryable failure exhausted request_id=%s after %s attempts: %s",
            job.request_id,
            job.worker_attempt_count,
            message,
        )
        return

    runtime.requeue_job(job, last_error=message)
    logger.exception(
        "Retryable failure requeued request_id=%s after attempt %s: %s",
        job.request_id,
        job.worker_attempt_count,
        message,
    )


def _ensure_archive_best_effort(
    runtime: SupabaseJudgeApiRuntime,
    job: JudgeApiJob,
) -> ArchivedJudgeImage | None:
    try:
        archived_image = runtime.ensure_archived_input_image(job)
    except RetryableWorkerError as exc:
        logger.warning("Failed to archive input for request_id=%s during terminal fallback: %s", job.request_id, exc)
        return None
    _log_archived_input(archived_image, job)
    return archived_image


def _delete_input_object_best_effort(runtime: SupabaseJudgeApiRuntime, job: JudgeApiJob) -> None:
    try:
        runtime.delete_input_object(job)
        logger.info(
            "Deleted transient Supabase input for request_id=%s from %s/%s.",
            job.request_id,
            job.input_bucket or runtime.config.input_bucket,
            job.input_storage_path,
        )
    except RetryableWorkerError as exc:
        logger.warning("Failed to delete input object for request_id=%s: %s", job.request_id, exc)


def _log_archived_input(archived_image: ArchivedJudgeImage, job: JudgeApiJob) -> None:
    logger.info(
        "Archived judged input for request_id=%s at %s (%s).",
        job.request_id,
        archived_image.local_path,
        archived_image.filename,
    )


if __name__ == "__main__":
    main()
