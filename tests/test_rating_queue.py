from __future__ import annotations

import unittest
from dataclasses import dataclass
from unittest.mock import Mock
from uuid import UUID

import httpx

from deploy.rating_queue import (
    DatabaseError,
    JudgeApiClient,
    JudgeApiError,
    PostgresRatingQueueClient,
    QueueMessage,
    QueueWorkerConfig,
    RatingJob,
    RatingJobPayload,
    RatingQueueListener,
    RetryableJudgeApiError,
    UNRATED_IMAGE_MESSAGE,
    UnratedImageError,
    build_score_log_payload,
    normalize_judge_api_result,
    normalize_queue_message,
)
from deploy.rating_queue_worker import drain_visible_queue_messages, process_queue_message


def build_config() -> QueueWorkerConfig:
    return QueueWorkerConfig(
        supabase_db_url="postgresql://postgres:postgres@localhost:5432/postgres",
        queue_name="rating_dispatch",
        notify_channel="rating_queue_wakeup",
        batch_size=25,
        visibility_timeout_seconds=300,
        stale_after_seconds=300,
        idle_reconcile_seconds=300,
        max_retries=3,
        graffiti_api_url="http://127.0.0.1:8000",
        graffiti_api_token="auth-token",
    )


def build_job(engine_id: str = "judge-api-v1") -> RatingJob:
    return RatingJob(
        id="job-123",
        request_id="req-123",
        status="processing",
        judgement_engine_id=engine_id,
        payload=RatingJobPayload(
            image_data_url="data:image/jpeg;base64,ZmFrZQ==",
            image_file_name="piece.jpg",
            image_hash_sha256="abc123",
            image_mime_type="image/jpeg",
            image_size_bytes=123456,
            requested_critique_language="English",
            requested_judgement_engine_id=engine_id,
            requested_judgement_model=None,
        ),
    )


@dataclass(frozen=True)
class FakeNotification:
    payload: str


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self._rows: list[dict[str, object]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, query, params=None) -> None:
        self.connection.executed.append((str(query), params))
        if self.connection.results:
            self._rows = self.connection.results.pop(0)
        else:
            self._rows = []

    def fetchall(self) -> list[dict[str, object]]:
        return list(self._rows)


class FakeConnection:
    def __init__(
        self,
        *,
        results: list[list[dict[str, object]]] | None = None,
        notifications: list[FakeNotification] | None = None,
    ) -> None:
        self.results = list(results or [])
        self.notifications = list(notifications or [])
        self.executed: list[tuple[str, object]] = []
        self.notify_calls: list[tuple[int, int | None]] = []
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True

    def notifies(self, *, timeout: int, stop_after: int | None = None):
        self.notify_calls.append((timeout, stop_after))
        yielded = 0
        while self.notifications and (stop_after is None or yielded < stop_after):
            yielded += 1
            yield self.notifications.pop(0)


class NormalizeJudgeApiResultTests(unittest.TestCase):
    def test_normalizes_predict_payload_to_stored_rating_result(self) -> None:
        result = normalize_judge_api_result(
            {
                "image_usable": True,
                "medium": "wall_piece",
                "overall_score": 76,
                "legibility": 82,
                "letter_structure": 8.2,
                "line_quality": 7.8,
                "composition": 7.1,
                "color_harmony": 66,
                "originality": 8.7,
                "request_id": "predict-123",
            }
        )

        self.assertEqual(result["totalScore"], 7.6)
        self.assertEqual(
            result["categoryScores"],
            {
                "legibility": 8,
                "letterStructure": 8,
                "lineQuality": 8,
                "composition": 7,
                "colorHarmony": 7,
                "originality": 9,
            },
        )
        self.assertEqual(result["critique"], "")
        self.assertEqual(result["ratingSchemaVersion"], "v2")
        self.assertEqual(result["judgementEngineId"], "judge-api-v1")
        self.assertEqual(result["requestId"], "predict-123")

    def test_unrated_payload_raises_expected_message(self) -> None:
        with self.assertRaisesRegex(UnratedImageError, UNRATED_IMAGE_MESSAGE):
            normalize_judge_api_result(
                {
                    "image_usable": True,
                    "medium": "digital",
                    "overall_score": 7,
                    "legibility": 7,
                    "letter_structure": 7,
                    "line_quality": 7,
                    "composition": 7,
                    "color_harmony": 7,
                    "originality": 7,
                }
            )

        with self.assertRaisesRegex(UnratedImageError, UNRATED_IMAGE_MESSAGE):
            normalize_judge_api_result(
                {
                    "image_usable": True,
                    "medium": "wall_piece",
                    "overall_score": 7,
                    "legibility": 7,
                    "letter_structure": 7,
                    "line_quality": 7,
                    "composition": 7,
                    "color_harmony": None,
                    "originality": 7,
                }
            )


class QueueClientTests(unittest.TestCase):
    def test_read_visible_messages_uses_pgmq_read(self) -> None:
        connection = FakeConnection(
            results=[
                [
                    {
                        "msg_id": 5,
                        "read_ct": 2,
                        "message": {"jobId": "job-queue", "requestId": "req-queue"},
                    }
                ]
            ]
        )
        queue_client = PostgresRatingQueueClient(build_config(), connection=connection)

        messages = queue_client.read_visible_messages()

        self.assertEqual(messages, [QueueMessage(msg_id=5, read_count=2, job_id="job-queue", request_id="req-queue")])
        self.assertEqual(connection.executed[0][0], "select * from pgmq.read(%s, %s, %s)")
        self.assertEqual(connection.executed[0][1], ("rating_dispatch", 300, 25))

    def test_claim_rating_job_uses_claim_rpc(self) -> None:
        connection = FakeConnection(
            results=[
                [
                    {
                        "id": UUID("00000000-0000-0000-0000-000000000123"),
                        "request_id": "req-claim",
                        "status": "processing",
                        "judgement_engine_id": "judge-api-v1",
                        "request_payload": {
                            "imageDataUrl": "data:image/jpeg;base64,ZmFrZQ==",
                            "imageFileName": "piece.jpg",
                            "imageHashSha256": "abc123",
                            "imageMimeType": "image/jpeg",
                            "imageSizeBytes": 123456,
                            "requestedCritiqueLanguage": "English",
                            "requestedJudgementEngineId": "judge-api-v1",
                            "requestedJudgementModel": None,
                        },
                    }
                ]
            ]
        )
        queue_client = PostgresRatingQueueClient(build_config(), connection=connection)

        job = queue_client.claim_rating_job("job-claim")

        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.id, "00000000-0000-0000-0000-000000000123")
        self.assertEqual(connection.executed[0][0], "select * from public.claim_rating_job(%s::uuid, %s)")
        self.assertEqual(connection.executed[0][1], ("job-claim", 300))

    def test_complete_rating_job_uses_completion_rpc(self) -> None:
        connection = FakeConnection(results=[[{"id": "job-123"}]])
        queue_client = PostgresRatingQueueClient(build_config(), connection=connection)

        queue_client.complete_rating_job(
            "job-123",
            {"totalScore": 7.5},
            {"total_score": 7.5, "critique_language": "English"},
        )

        self.assertEqual(connection.executed[0][0], "select * from public.complete_rating_job(%s::uuid, %s, %s)")
        self.assertEqual(connection.executed[0][1][0], "job-123")
        self.assertEqual(connection.executed[0][1][1].obj, {"totalScore": 7.5})
        self.assertEqual(
            connection.executed[0][1][2].obj,
            {"total_score": 7.5, "critique_language": "English"},
        )

    def test_listener_waits_for_notification(self) -> None:
        connection = FakeConnection(notifications=[FakeNotification("job-wakeup")])
        listener = RatingQueueListener(build_config(), connection=connection)

        payload = listener.wait_for_notification(300)

        self.assertEqual(payload, "job-wakeup")
        self.assertTrue(any("LISTEN" in query for query, _ in connection.executed))
        self.assertEqual(connection.notify_calls, [(300, 1)])

    def test_normalize_queue_message_accepts_expected_shape(self) -> None:
        message = normalize_queue_message(
            {
                "msg_id": 9,
                "read_ct": 1,
                "message": {"jobId": "job-9", "requestId": "req-9"},
            }
        )

        self.assertEqual(message.job_id, "job-9")
        self.assertEqual(message.request_id, "req-9")


class JudgeApiClientTests(unittest.TestCase):
    def test_process_rating_job_rejects_unsupported_engine(self) -> None:
        judge_api_client = JudgeApiClient(
            build_config(),
            client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
        )

        with self.assertRaisesRegex(JudgeApiError, "Unsupported requestedJudgementEngineId"):
            judge_api_client.process_rating_job(build_job("single-pass-v1"))

    def test_process_rating_job_marks_transport_failures_retryable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"message": "upstream unavailable"})

        judge_api_client = JudgeApiClient(
            build_config(),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        with self.assertRaisesRegex(RetryableJudgeApiError, "upstream unavailable"):
            judge_api_client.process_rating_job(build_job())


class RatingQueueWorkerTests(unittest.TestCase):
    def test_build_score_log_payload_maps_result_into_rating_scores_shape(self) -> None:
        payload = build_score_log_payload(
            build_job(),
            {
                "totalScore": 7.6,
                "categoryScores": {
                    "legibility": 8,
                    "letterStructure": 8,
                    "lineQuality": 8,
                    "composition": 7,
                    "colorHarmony": 7,
                    "originality": 9,
                },
                "critique": "",
                "judgementEngineId": "judge-api-v1",
                "ratingSchemaVersion": "v2",
                "requestId": "predict-123",
            },
        )

        self.assertEqual(payload["total_score"], 7.6)
        self.assertEqual(payload["lettering_style"], 8)
        self.assertEqual(payload["color"], 7)
        self.assertEqual(payload["composition"], 7)
        self.assertEqual(payload["originality_concept"], 9)
        self.assertEqual(payload["technique_complexity"], 8)
        self.assertEqual(payload["critique_language"], "English")
        self.assertEqual(payload["image_mime_type"], "image/jpeg")
        self.assertEqual(payload["image_size_bytes"], 123456)
        self.assertEqual(payload["model"], "judge-api-v1")
        self.assertEqual(payload["request_id"], "req-123")

    def test_process_queue_message_marks_completed(self) -> None:
        queue_client = Mock()
        judge_api_client = Mock()
        queue_client.claim_rating_job.return_value = build_job()
        judge_api_client.process_rating_job.return_value = {
            "totalScore": 7.5,
            "categoryScores": {
                "legibility": 8,
                "letterStructure": 7,
                "lineQuality": 7,
                "composition": 7,
                "colorHarmony": 6,
                "originality": 8,
            },
            "critique": "",
            "judgementEngineId": "judge-api-v1",
            "ratingSchemaVersion": "v2",
        }

        process_queue_message(queue_client, judge_api_client, build_config(), QueueMessage(1, 1, "job-123", "req-123"))

        queue_client.complete_rating_job.assert_called_once()
        completion_args = queue_client.complete_rating_job.call_args.args
        self.assertEqual(completion_args[0], "job-123")
        self.assertEqual(completion_args[1]["totalScore"], 7.5)
        self.assertEqual(completion_args[2]["critique_language"], "English")
        self.assertEqual(completion_args[2]["request_id"], "req-123")
        queue_client.archive_message.assert_called_once_with(1)

    def test_process_queue_message_retries_retryable_failures(self) -> None:
        queue_client = Mock()
        judge_api_client = Mock()
        queue_client.claim_rating_job.return_value = build_job()
        judge_api_client.process_rating_job.side_effect = RetryableJudgeApiError("timed out")

        process_queue_message(queue_client, judge_api_client, build_config(), QueueMessage(4, 2, "job-123", "req-123"))

        queue_client.mark_job_retryable.assert_called_once_with("job-123")
        queue_client.set_message_visibility_timeout.assert_called_once_with(4, 10)
        queue_client.mark_job_failed.assert_not_called()

    def test_process_queue_message_fails_after_max_retries(self) -> None:
        queue_client = Mock()
        judge_api_client = Mock()
        queue_client.claim_rating_job.return_value = build_job()
        judge_api_client.process_rating_job.side_effect = RetryableJudgeApiError("timed out")

        process_queue_message(queue_client, judge_api_client, build_config(), QueueMessage(5, 3, "job-123", "req-123"))

        queue_client.mark_job_failed.assert_called_once_with("job-123", "timed out")
        queue_client.archive_message.assert_called_once_with(5)

    def test_process_queue_message_archives_terminal_duplicates(self) -> None:
        queue_client = Mock()
        judge_api_client = Mock()
        queue_client.claim_rating_job.return_value = None
        queue_client.load_job_state.return_value = Mock(status="completed")

        process_queue_message(queue_client, judge_api_client, build_config(), QueueMessage(6, 1, "job-123", "req-123"))

        queue_client.archive_message.assert_called_once_with(6)
        queue_client.set_message_visibility_timeout.assert_not_called()
        judge_api_client.process_rating_job.assert_not_called()

    def test_drain_visible_queue_messages_drains_until_empty(self) -> None:
        queue_client = Mock()
        judge_api_client = Mock()
        queue_client.read_visible_messages.side_effect = [
            [QueueMessage(1, 1, "job-1", "req-1"), QueueMessage(2, 1, "job-2", "req-2")],
            [],
        ]
        queue_client.claim_rating_job.side_effect = [build_job(), build_job()]
        judge_api_client.process_rating_job.return_value = {
            "totalScore": 7.5,
            "categoryScores": {
                "legibility": 8,
                "letterStructure": 7,
                "lineQuality": 7,
                "composition": 7,
                "colorHarmony": 6,
                "originality": 8,
            },
            "critique": "",
            "judgementEngineId": "judge-api-v1",
            "ratingSchemaVersion": "v2",
        }

        drained = drain_visible_queue_messages(queue_client, judge_api_client, build_config())

        self.assertEqual(drained, 2)
        self.assertEqual(queue_client.archive_message.call_count, 2)

    def test_process_queue_message_leaves_message_unarchived_when_completion_write_fails(self) -> None:
        queue_client = Mock()
        judge_api_client = Mock()
        queue_client.claim_rating_job.return_value = build_job()
        judge_api_client.process_rating_job.return_value = {
            "totalScore": 7.5,
            "categoryScores": {
                "legibility": 8,
                "letterStructure": 7,
                "lineQuality": 7,
                "composition": 7,
                "colorHarmony": 6,
                "originality": 8,
            },
            "critique": "",
            "judgementEngineId": "judge-api-v1",
            "ratingSchemaVersion": "v2",
        }
        queue_client.complete_rating_job.side_effect = DatabaseError("write failed")

        with self.assertRaisesRegex(DatabaseError, "write failed"):
            process_queue_message(queue_client, judge_api_client, build_config(), QueueMessage(7, 1, "job-123", "req-123"))

        queue_client.archive_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
