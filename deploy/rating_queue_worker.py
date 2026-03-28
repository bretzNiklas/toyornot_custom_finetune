from __future__ import annotations

import logging
import time

from .rating_queue import (
    build_score_log_payload,
    DatabaseError,
    InvalidQueueMessageError,
    JudgeApiClient,
    JudgeApiError,
    PostgresRatingQueueClient,
    QueueMessage,
    QueueWorkerConfig,
    RatingQueueError,
    RatingQueueListener,
    RetryableJudgeApiError,
    build_retry_delay_seconds,
)


logger = logging.getLogger("graffiti_rating_queue_worker")
logging.basicConfig(level=logging.INFO)


def process_queue_message(
    queue_client: PostgresRatingQueueClient,
    judge_api_client: JudgeApiClient,
    config: QueueWorkerConfig,
    message: QueueMessage,
) -> None:
    try:
        job = queue_client.claim_rating_job(message.job_id)
    except DatabaseError:
        raise
    except RatingQueueError as exc:
        logger.warning("Rating job %s could not be claimed: %s", message.job_id, exc)
        queue_client.mark_job_failed(message.job_id, str(exc))
        queue_client.archive_message(message.msg_id)
        return

    if job is None:
        job_state = queue_client.load_job_state(message.job_id)
        if job_state is None:
            logger.warning("Queue message %s referenced missing rating job %s; archiving.", message.msg_id, message.job_id)
            queue_client.archive_message(message.msg_id)
            return

        if job_state.status in {"completed", "failed"}:
            logger.info(
                "Queue message %s referenced terminal rating job %s (%s); archiving duplicate delivery.",
                message.msg_id,
                message.job_id,
                job_state.status,
            )
            queue_client.archive_message(message.msg_id)
            return

        logger.info(
            "Queue message %s hit non-claimable rating job %s (%s); extending visibility timeout.",
            message.msg_id,
            message.job_id,
            job_state.status,
        )
        queue_client.set_message_visibility_timeout(message.msg_id, config.visibility_timeout_seconds)
        return

    logger.info(
        "Processing queue message %s for rating job %s (attempt %s).",
        message.msg_id,
        job.id,
        message.read_count,
    )

    try:
        result_payload = judge_api_client.process_rating_job(job)
    except RetryableJudgeApiError as exc:
        if message.read_count >= config.max_retries:
            logger.warning(
                "Retryable rating job %s exhausted %s attempt(s): %s",
                job.id,
                config.max_retries,
                exc,
            )
            queue_client.mark_job_failed(job.id, str(exc))
            queue_client.archive_message(message.msg_id)
            return

        retry_delay_seconds = build_retry_delay_seconds(message.read_count)
        logger.warning(
            "Retryable rating job %s failed on attempt %s; requeueing for retry in %ss: %s",
            job.id,
            message.read_count,
            retry_delay_seconds,
            exc,
        )
        queue_client.mark_job_retryable(job.id)
        queue_client.set_message_visibility_timeout(message.msg_id, retry_delay_seconds)
        return
    except JudgeApiError as exc:
        logger.warning("Rating job %s failed permanently: %s", job.id, exc)
        queue_client.mark_job_failed(job.id, str(exc))
        queue_client.archive_message(message.msg_id)
        return
    except Exception as exc:
        logger.exception("Rating job %s failed unexpectedly: %s", job.id, exc)
        queue_client.mark_job_failed(job.id, str(exc))
        queue_client.archive_message(message.msg_id)
        return

    score_log_payload = build_score_log_payload(job, result_payload)
    queue_client.complete_rating_job(job.id, result_payload, score_log_payload)
    queue_client.archive_message(message.msg_id)
    logger.info("Completed rating job %s.", job.id)


def drain_visible_queue_messages(
    queue_client: PostgresRatingQueueClient,
    judge_api_client: JudgeApiClient,
    config: QueueWorkerConfig,
) -> int:
    processed_messages = 0

    while True:
        messages = queue_client.read_visible_messages()
        if not messages:
            return processed_messages

        logger.info("Fetched %s queue message(s) from %s.", len(messages), config.queue_name)
        for message in messages:
            processed_messages += 1
            try:
                process_queue_message(queue_client, judge_api_client, config, message)
            except InvalidQueueMessageError as exc:
                logger.warning("Discarding invalid queue message %s: %s", message.msg_id, exc)
                queue_client.archive_message(message.msg_id)


def run_worker_forever() -> None:
    config = QueueWorkerConfig.from_env()
    logger.info(
        "Starting event-driven rating queue worker for queue=%s channel=%s api=%s.",
        config.queue_name,
        config.notify_channel,
        config.graffiti_api_url,
    )

    while True:
        queue_client: PostgresRatingQueueClient | None = None
        listener: RatingQueueListener | None = None
        judge_api_client: JudgeApiClient | None = None

        try:
            queue_client = PostgresRatingQueueClient(config)
            listener = RatingQueueListener(config)
            judge_api_client = JudgeApiClient(config)

            drained_messages = drain_visible_queue_messages(queue_client, judge_api_client, config)
            if drained_messages > 0:
                logger.info("Drained %s backlog queue message(s) on startup.", drained_messages)

            while True:
                payload = listener.wait_for_notification(config.idle_reconcile_seconds)
                if payload:
                    logger.info("Received queue wake-up notification for rating job %s.", payload)
                else:
                    logger.info(
                        "No queue wake-up received within %ss; running idle reconciliation drain.",
                        config.idle_reconcile_seconds,
                    )

                drained_messages = drain_visible_queue_messages(queue_client, judge_api_client, config)
                if payload and drained_messages == 0:
                    logger.info("Wake-up notification arrived but no visible queue messages were ready.")
        except DatabaseError as exc:
            logger.exception("Rating queue database loop failed: %s", exc)
            time.sleep(3)
        except Exception as exc:
            logger.exception("Rating queue worker loop failed: %s", exc)
            time.sleep(3)
        finally:
            if listener is not None:
                listener.close()
            if queue_client is not None:
                queue_client.close()
            if judge_api_client is not None:
                judge_api_client.close()


def main() -> None:
    run_worker_forever()


if __name__ == "__main__":
    main()
