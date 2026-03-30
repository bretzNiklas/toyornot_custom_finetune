from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from deploy.judge_api_handoff_runtime import (
    ArchivedJudgeImage,
    JUDGE_JOB_STATUS_CLAIMED,
    JUDGE_JOB_STATUS_FAILED,
    JUDGE_JOB_STATUS_PENDING,
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
    _as_optional_datetime,
    _as_optional_str,
    build_result_row_from_error,
    build_result_row_from_success,
    extract_error_message,
)


logger = logging.getLogger("graffiti_judge_handoff_worker")
logging.basicConfig(level=logging.INFO)

REALTIME_CHANNEL_TOPIC = "judge-api-job-activation"
REALTIME_SCHEMA = "public"
REALTIME_RETRY_DELAY_SECONDS = 5.0


async def create_realtime_activation_client(config: JudgeApiHandoffConfig) -> Any:
    from supabase import AsyncClientOptions, create_async_client

    return await create_async_client(
        config.supabase_url,
        config.supabase_service_role_key,
        options=AsyncClientOptions(realtime={"auto_reconnect": True}),
    )


def process_handoff_job(
    runtime: SupabaseJudgeApiRuntime,
    piecerate: PiecerateClient,
    config: JudgeApiHandoffConfig,
    job: JudgeApiJob,
) -> None:
    archived_image: ArchivedJudgeImage | None = None
    last_lock_refresh_at = _as_optional_datetime(job.locked_at)
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
            last_lock_refresh_at = _utc_now()
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
                now = _utc_now()
                if _should_refresh_job_lock(
                    last_lock_refresh_at,
                    now=now,
                    lock_refresh_seconds=config.lock_refresh_seconds,
                ):
                    runtime.refresh_job_lock(
                        job,
                        status=JUDGE_JOB_STATUS_PROCESSING,
                        piecerate_job_id=piecerate_job_id,
                        piecerate_request_id=piecerate_request_id,
                    )
                    last_lock_refresh_at = now
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


def drain_ready_jobs(
    runtime: SupabaseJudgeApiRuntime,
    piecerate: PiecerateClient,
    config: JudgeApiHandoffConfig,
) -> int:
    processed_count = 0
    while run_worker_iteration(runtime, piecerate, config):
        processed_count += 1
    return processed_count


class HandoffWorkerCoordinator:
    def __init__(
        self,
        runtime: SupabaseJudgeApiRuntime,
        piecerate: PiecerateClient,
        config: JudgeApiHandoffConfig,
        stop_event: asyncio.Event,
        *,
        realtime_client_factory: Callable[[JudgeApiHandoffConfig], Awaitable[Any]] = create_realtime_activation_client,
    ) -> None:
        self.runtime = runtime
        self.piecerate = piecerate
        self.config = config
        self.stop_event = stop_event
        self.realtime_client_factory = realtime_client_factory
        self.wake_event = asyncio.Event()
        self.retry_timer_task: asyncio.Task[None] | None = None
        self.next_retry_at: datetime | None = None
        self._seen_realtime_subscription = False

    async def run(self) -> None:
        await self._drain_ready_jobs("startup")
        await self._refresh_retry_timer()

        dispatch_task = asyncio.create_task(self._dispatch_loop())
        realtime_task = asyncio.create_task(self._run_realtime_listener())
        safety_task = asyncio.create_task(self._run_safety_sweep())

        try:
            await self.stop_event.wait()
        finally:
            await self._cancel_retry_timer()
            for task in (dispatch_task, realtime_task, safety_task):
                task.cancel()
            for task in (dispatch_task, realtime_task, safety_task):
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def request_drain(self, reason: str) -> None:
        logger.info("Scheduling handoff drain due to %s.", reason)
        self.wake_event.set()

    async def _dispatch_loop(self) -> None:
        while not self.stop_event.is_set():
            await self.wake_event.wait()
            while self.wake_event.is_set() and not self.stop_event.is_set():
                self.wake_event.clear()
                await self._drain_ready_jobs("activation")
                await self._refresh_retry_timer()

    async def _drain_ready_jobs(self, reason: str) -> None:
        try:
            processed_count = await asyncio.to_thread(
                drain_ready_jobs,
                self.runtime,
                self.piecerate,
                self.config,
            )
        except Exception as exc:
            logger.exception("Handoff worker drain failed during %s: %s", reason, exc)
            return
        logger.info("Handoff drain %s processed %s ready job(s).", reason, processed_count)

    async def _refresh_retry_timer(self) -> None:
        try:
            next_retry_at = await asyncio.to_thread(self.runtime.get_next_pending_retry_at)
        except RetryableWorkerError as exc:
            logger.exception("Failed to refresh next retry timer: %s", exc)
            return
        self._schedule_retry_timer(next_retry_at, source="supabase_refresh")

    async def _cancel_retry_timer(self) -> None:
        if self.retry_timer_task is None:
            self.next_retry_at = None
            return
        self.retry_timer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.retry_timer_task
        self.retry_timer_task = None
        self.next_retry_at = None

    def _schedule_retry_timer(self, next_retry_at: datetime | None, *, source: str) -> None:
        if next_retry_at is None:
            if self.retry_timer_task is not None:
                self.retry_timer_task.cancel()
                self.retry_timer_task = None
            self.next_retry_at = None
            return

        next_retry_at = _ensure_utc(next_retry_at)
        delay_seconds = (next_retry_at - datetime.now(timezone.utc)).total_seconds()
        if delay_seconds <= 0:
            self.next_retry_at = None
            if self.retry_timer_task is not None:
                self.retry_timer_task.cancel()
                self.retry_timer_task = None
            self.request_drain(source)
            return

        if self.next_retry_at is not None and next_retry_at >= self.next_retry_at:
            return

        if self.retry_timer_task is not None:
            self.retry_timer_task.cancel()

        self.next_retry_at = next_retry_at
        self.retry_timer_task = asyncio.create_task(self._run_retry_timer(next_retry_at, delay_seconds))
        logger.info("Armed local retry timer for %s via %s.", next_retry_at.isoformat(), source)

    async def _run_retry_timer(self, next_retry_at: datetime, delay_seconds: float) -> None:
        try:
            await asyncio.sleep(delay_seconds)
        except asyncio.CancelledError:
            raise
        finally:
            if self.next_retry_at == next_retry_at:
                self.next_retry_at = None
                self.retry_timer_task = None
        self.request_drain("next_attempt_at")

    async def _run_safety_sweep(self) -> None:
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(),
                    timeout=self.config.safety_sweep_seconds,
                )
            except TimeoutError:
                self.request_drain("safety_sweep")

    async def _run_realtime_listener(self) -> None:
        while not self.stop_event.is_set():
            realtime_client = None
            channel = None
            listen_task: asyncio.Task[None] | None = None
            stop_wait_task: asyncio.Task[bool] | None = None
            try:
                realtime_client = await self.realtime_client_factory(self.config)
                channel = realtime_client.channel(REALTIME_CHANNEL_TOPIC)
                channel.on_postgres_changes(
                    "INSERT",
                    table=self.config.jobs_table,
                    schema=REALTIME_SCHEMA,
                    callback=self._handle_realtime_payload,
                )
                channel.on_postgres_changes(
                    "UPDATE",
                    table=self.config.jobs_table,
                    schema=REALTIME_SCHEMA,
                    callback=self._handle_realtime_payload,
                )
                await channel.subscribe(self._handle_realtime_status)
                listen_task = asyncio.create_task(realtime_client.realtime.listen())
                stop_wait_task = asyncio.create_task(self.stop_event.wait())
                done, pending = await asyncio.wait(
                    {listen_task, stop_wait_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for pending_task in pending:
                    pending_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await pending_task
                if stop_wait_task in done:
                    return
                if listen_task in done:
                    listen_task.result()
                    self.request_drain("realtime_listener_exit")
                    await self._sleep_until_stop(REALTIME_RETRY_DELAY_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Realtime activation loop failed: %s", exc)
                self.request_drain("realtime_activation_failure")
                await self._sleep_until_stop(REALTIME_RETRY_DELAY_SECONDS)
            finally:
                if realtime_client is not None and channel is not None:
                    with contextlib.suppress(Exception):
                        await realtime_client.remove_channel(channel)
                if listen_task is not None and not listen_task.done():
                    listen_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await listen_task

    async def _sleep_until_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except TimeoutError:
            return

    def _handle_realtime_status(self, status: Any, err: Exception | None) -> None:
        status_value = _as_optional_str(getattr(status, "value", status))
        if status_value == "SUBSCRIBED":
            reconnect = self._seen_realtime_subscription
            self._seen_realtime_subscription = True
            self.request_drain("realtime_reconnect" if reconnect else "realtime_subscribed")
            return
        if status_value in {"CHANNEL_ERROR", "TIMED_OUT", "CLOSED"}:
            if err is None:
                logger.warning("Realtime activation status changed to %s.", status_value)
            else:
                logger.warning("Realtime activation status changed to %s: %s", status_value, err)

    def _handle_realtime_payload(self, payload: dict[str, Any]) -> None:
        record = _extract_realtime_record(payload)
        if record is None:
            self.request_drain("realtime_unparsed_payload")
            return

        status = _as_optional_str(record.get("status"))
        if status != JUDGE_JOB_STATUS_PENDING:
            return

        next_attempt_at = _as_optional_datetime(record.get("next_attempt_at"))
        if next_attempt_at is None:
            self.request_drain(_extract_realtime_reason(payload))
            return

        self._schedule_retry_timer(_ensure_utc(next_attempt_at), source=_extract_realtime_reason(payload))


async def run_worker_service(
    config: JudgeApiHandoffConfig | None = None,
    *,
    stop_event: asyncio.Event | None = None,
    runtime: SupabaseJudgeApiRuntime | None = None,
    piecerate: PiecerateClient | None = None,
    realtime_client_factory: Callable[[JudgeApiHandoffConfig], Awaitable[Any]] = create_realtime_activation_client,
) -> None:
    worker_config = config or JudgeApiHandoffConfig.from_env()
    runtime = runtime or SupabaseJudgeApiRuntime(worker_config)
    piecerate = piecerate or PiecerateClient(worker_config)
    stop_event = stop_event or asyncio.Event()
    coordinator = HandoffWorkerCoordinator(
        runtime,
        piecerate,
        worker_config,
        stop_event,
        realtime_client_factory=realtime_client_factory,
    )
    try:
        await coordinator.run()
    finally:
        close = getattr(piecerate, "close", None)
        if callable(close):
            close()


def run_worker_forever(config: JudgeApiHandoffConfig | None = None) -> None:
    worker_config = config or JudgeApiHandoffConfig.from_env()

    async def runner() -> None:
        stop_event = asyncio.Event()
        previous_handlers = {
            signal.SIGTERM: signal.getsignal(signal.SIGTERM),
            signal.SIGINT: signal.getsignal(signal.SIGINT),
        }

        def handle_signal(signum, frame) -> None:  # type: ignore[no-untyped-def]
            logger.info("Received signal %s, stopping handoff worker.", signum)
            stop_event.set()

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)
        try:
            await run_worker_service(worker_config, stop_event=stop_event)
        finally:
            for signum, previous in previous_handlers.items():
                signal.signal(signum, previous)

    asyncio.run(runner())


def main() -> None:
    run_worker_forever()


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _should_refresh_job_lock(
    last_lock_refresh_at: datetime | None,
    *,
    now: datetime,
    lock_refresh_seconds: int,
) -> bool:
    if last_lock_refresh_at is None:
        return True
    elapsed_seconds = (now - _ensure_utc(last_lock_refresh_at)).total_seconds()
    return elapsed_seconds >= lock_refresh_seconds


def _extract_realtime_record(payload: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("record", "new"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("record", "new"):
            value = data.get(key)
            if isinstance(value, dict):
                return value
    return None


def _extract_realtime_reason(payload: dict[str, Any]) -> str:
    data = payload.get("data")
    for mapping in (payload, data if isinstance(data, dict) else None):
        if not isinstance(mapping, dict):
            continue
        for key in ("eventType", "type", "event"):
            value = _as_optional_str(mapping.get(key))
            if value:
                return f"realtime_{value.lower()}"
    return "realtime_change"


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
