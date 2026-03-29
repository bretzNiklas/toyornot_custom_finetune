from __future__ import annotations

import json
import math
import os
import socket
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4


JOB_STATUS_QUEUED = "queued"
JOB_STATUS_PROCESSING = "processing"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
TERMINAL_JOB_STATUSES = {JOB_STATUS_COMPLETED, JOB_STATUS_FAILED}


@dataclass(frozen=True)
class QueueConfig:
    runtime_root: Path
    jobs_db_path: Path
    spool_dir: Path
    worker_concurrency: int
    job_lease_seconds: int
    max_retries: int
    max_estimated_wait_seconds: int
    job_retention_hours: int
    worker_heartbeat_timeout_seconds: int
    worker_heartbeat_interval_seconds: int
    worker_idle_poll_seconds: float
    default_processing_seconds: float
    processing_average_window: int
    orphan_payload_grace_seconds: int

    @classmethod
    def from_env(cls) -> "QueueConfig":
        runtime_root = Path(os.environ.get("RUNTIME_ROOT", "/srv/graffiti-student/runtime"))
        jobs_db_path = Path(os.environ.get("JOBS_DB_PATH", str(runtime_root / "jobs.sqlite3")))
        spool_dir = Path(os.environ.get("JOB_SPOOL_DIR", str(runtime_root / "spool")))
        return cls(
            runtime_root=runtime_root,
            jobs_db_path=jobs_db_path,
            spool_dir=spool_dir,
            worker_concurrency=_positive_int_env("WORKER_CONCURRENCY", 1),
            job_lease_seconds=_positive_int_env("JOB_LEASE_SECONDS", 30),
            max_retries=_non_negative_int_env("MAX_RETRIES", 2),
            max_estimated_wait_seconds=_positive_int_env("MAX_ESTIMATED_WAIT_SECONDS", 90),
            job_retention_hours=_positive_int_env("JOB_RETENTION_HOURS", 24),
            worker_heartbeat_timeout_seconds=_positive_int_env("WORKER_HEARTBEAT_TIMEOUT_SECONDS", 45),
            worker_heartbeat_interval_seconds=_positive_int_env("WORKER_HEARTBEAT_INTERVAL_SECONDS", 5),
            worker_idle_poll_seconds=_positive_float_env("WORKER_IDLE_POLL_SECONDS", 0.5),
            default_processing_seconds=_positive_float_env("DEFAULT_PROCESSING_SECONDS", 5.0),
            processing_average_window=_positive_int_env("PROCESSING_AVERAGE_WINDOW", 20),
            orphan_payload_grace_seconds=_positive_int_env("ORPHAN_PAYLOAD_GRACE_SECONDS", 300),
        )

    @property
    def total_allowed_attempts(self) -> int:
        return self.max_retries + 1


@dataclass(frozen=True)
class QueueAdmission:
    accepted: bool
    job_id: str
    request_id: str
    status: str
    queue_position: int
    estimated_wait_seconds: int


@dataclass(frozen=True)
class QueueSnapshot:
    queued_jobs: int
    processing_jobs: int
    average_processing_seconds: float


@dataclass(frozen=True)
class WorkerHeartbeatSnapshot:
    fresh_worker_count: int
    total_worker_rows: int
    most_recent_heartbeat_age_seconds: float | None


@dataclass(frozen=True)
class QueueHealthSnapshot:
    queued_jobs: int
    processing_jobs: int
    oldest_queued_age_seconds: float | None
    average_processing_seconds: float
    fresh_worker_count: int
    total_worker_rows: int
    most_recent_heartbeat_age_seconds: float | None
    worker_heartbeat_fresh: bool


@dataclass(frozen=True)
class JobRecord:
    row_id: int
    job_id: str
    request_id: str
    status: str
    created_at: float
    started_at: float | None
    completed_at: float | None
    lease_expires_at: float | None
    attempt_count: int
    filename: str | None
    include_debug: bool
    payload_path: str | None
    payload_size_bytes: int
    result_json: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    processing_duration_ms: float | None


class QueueOverloadedError(RuntimeError):
    def __init__(self, admission: QueueAdmission) -> None:
        self.admission = admission
        super().__init__("queue_overloaded")


def build_worker_id(index: int) -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{index}:{uuid4().hex[:8]}"


class LocalJobQueue:
    def __init__(self, config: QueueConfig | None = None) -> None:
        self.config = config or QueueConfig.from_env()

    def initialize(self) -> None:
        self.config.runtime_root.mkdir(parents=True, exist_ok=True)
        self.config.spool_dir.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                create table if not exists jobs (
                  row_id integer primary key autoincrement,
                  job_id text not null unique,
                  request_id text not null,
                  status text not null check (status in ('queued', 'processing', 'completed', 'failed')),
                  created_at real not null,
                  started_at real,
                  completed_at real,
                  lease_expires_at real,
                  attempt_count integer not null default 0,
                  filename text,
                  include_debug integer not null default 0,
                  payload_path text,
                  payload_size_bytes integer not null,
                  result_json text,
                  error_code text,
                  error_message text,
                  processing_duration_ms real
                );

                create index if not exists idx_jobs_status_created_at
                  on jobs (status, created_at asc, row_id asc);

                create index if not exists idx_jobs_lease_expires_at
                  on jobs (status, lease_expires_at asc);

                create index if not exists idx_jobs_completed_at
                  on jobs (completed_at asc);

                create table if not exists worker_heartbeats (
                  worker_id text primary key,
                  heartbeat_at real not null,
                  current_job_id text,
                  status text not null
                );
                """
            )

    def cleanup(self) -> None:
        cutoff = time.time() - (self.config.job_retention_hours * 3600)
        active_payloads = set()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                select payload_path
                from jobs
                where status in (?, ?) and payload_path is not null
                """,
                (JOB_STATUS_QUEUED, JOB_STATUS_PROCESSING),
            )
            active_payloads = {str(row["payload_path"]) for row in cursor.fetchall() if row["payload_path"]}
            connection.execute(
                """
                delete from jobs
                where status in (?, ?) and completed_at is not null and completed_at < ?
                """,
                (JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, cutoff),
            )
            connection.execute(
                """
                delete from worker_heartbeats
                where heartbeat_at < ?
                """,
                (time.time() - max(self.config.worker_heartbeat_timeout_seconds * 4, 60),),
            )

        orphan_cutoff = time.time() - self.config.orphan_payload_grace_seconds
        if not self.config.spool_dir.exists():
            return
        for payload_path in self.config.spool_dir.iterdir():
            if not payload_path.is_file():
                continue
            resolved = str(payload_path)
            if resolved in active_payloads:
                continue
            try:
                if payload_path.stat().st_mtime <= orphan_cutoff:
                    payload_path.unlink(missing_ok=True)
            except FileNotFoundError:
                continue

    def enqueue_job(
        self,
        *,
        job_id: str,
        request_id: str,
        filename: str | None,
        include_debug: bool,
        payload_path: Path,
        payload_size_bytes: int,
    ) -> QueueAdmission:
        with self._connection() as connection:
            connection.execute("begin immediate")
            snapshot = self._read_queue_snapshot(connection)
            estimated_wait_seconds = self._estimate_completion_seconds(
                queued_jobs=snapshot.queued_jobs,
                processing_jobs=snapshot.processing_jobs,
                average_processing_seconds=snapshot.average_processing_seconds,
                include_new_job=True,
            )
            queue_position = snapshot.queued_jobs + 1
            if estimated_wait_seconds > self.config.max_estimated_wait_seconds:
                connection.rollback()
                return QueueAdmission(
                    accepted=False,
                    job_id=job_id,
                    request_id=request_id,
                    status=JOB_STATUS_QUEUED,
                    queue_position=queue_position,
                    estimated_wait_seconds=estimated_wait_seconds,
                )

            connection.execute(
                """
                insert into jobs (
                  job_id,
                  request_id,
                  status,
                  created_at,
                  filename,
                  include_debug,
                  payload_path,
                  payload_size_bytes
                )
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    request_id,
                    JOB_STATUS_QUEUED,
                    time.time(),
                    filename,
                    1 if include_debug else 0,
                    str(payload_path),
                    payload_size_bytes,
                ),
            )
            connection.commit()
        return QueueAdmission(
            accepted=True,
            job_id=job_id,
            request_id=request_id,
            status=JOB_STATUS_QUEUED,
            queue_position=queue_position,
            estimated_wait_seconds=estimated_wait_seconds,
        )

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                select *
                from jobs
                where job_id = ?
                """,
                (job_id,),
            ).fetchone()
        return self._row_to_job(row)

    def get_job_queue_position(self, job: JobRecord) -> int:
        if job.status != JOB_STATUS_QUEUED:
            return 0
        with self._connection() as connection:
            row = connection.execute(
                """
                select count(*) as queued_before
                from jobs
                where status = ? and row_id <= ?
                """,
                (JOB_STATUS_QUEUED, job.row_id),
            ).fetchone()
        return int(row["queued_before"]) if row is not None else 0

    def estimate_job_wait_seconds(self, job: JobRecord) -> int:
        if job.status in TERMINAL_JOB_STATUSES:
            return 0
        with self._connection() as connection:
            snapshot = self._read_queue_snapshot(connection)
            if job.status == JOB_STATUS_PROCESSING:
                started_at = job.started_at or time.time()
                remaining = max(0.0, snapshot.average_processing_seconds - max(0.0, time.time() - started_at))
                return int(math.ceil(remaining))
            queued_before = self.get_job_queue_position(job) - 1
        return self._estimate_completion_seconds(
            queued_jobs=max(0, queued_before),
            processing_jobs=snapshot.processing_jobs,
            average_processing_seconds=snapshot.average_processing_seconds,
            include_new_job=True,
        )

    def claim_next_job(self, worker_id: str) -> JobRecord | None:
        now = time.time()
        with self._connection() as connection:
            connection.execute("begin immediate")
            self._mark_expired_jobs_failed(connection, now)
            row = connection.execute(
                """
                select *
                from jobs
                where
                  status = ?
                  or (
                    status = ?
                    and lease_expires_at is not null
                    and lease_expires_at <= ?
                    and attempt_count < ?
                  )
                order by
                  case when status = ? then 0 else 1 end asc,
                  row_id asc
                limit 1
                """,
                (
                    JOB_STATUS_QUEUED,
                    JOB_STATUS_PROCESSING,
                    now,
                    self.config.total_allowed_attempts,
                    JOB_STATUS_QUEUED,
                ),
            ).fetchone()
            if row is None:
                connection.commit()
                return None

            connection.execute(
                """
                update jobs
                set
                  status = ?,
                  started_at = ?,
                  completed_at = null,
                  lease_expires_at = ?,
                  attempt_count = attempt_count + 1,
                  error_code = null,
                  error_message = null
                where job_id = ?
                """,
                (
                    JOB_STATUS_PROCESSING,
                    now,
                    now + self.config.job_lease_seconds,
                    row["job_id"],
                ),
            )
            connection.execute(
                """
                insert into worker_heartbeats (worker_id, heartbeat_at, current_job_id, status)
                values (?, ?, ?, ?)
                on conflict(worker_id) do update set
                  heartbeat_at = excluded.heartbeat_at,
                  current_job_id = excluded.current_job_id,
                  status = excluded.status
                """,
                (worker_id, now, row["job_id"], JOB_STATUS_PROCESSING),
            )
            updated_row = connection.execute(
                """
                select *
                from jobs
                where job_id = ?
                """,
                (row["job_id"],),
            ).fetchone()
            connection.commit()
        return self._row_to_job(updated_row)

    def complete_job(
        self,
        *,
        job_id: str,
        result_payload: dict[str, Any],
        processing_duration_ms: float,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                update jobs
                set
                  status = ?,
                  completed_at = ?,
                  lease_expires_at = null,
                  result_json = ?,
                  error_code = null,
                  error_message = null,
                  processing_duration_ms = ?
                where job_id = ?
                """,
                (
                    JOB_STATUS_COMPLETED,
                    time.time(),
                    json.dumps(result_payload, ensure_ascii=True),
                    processing_duration_ms,
                    job_id,
                ),
            )

    def requeue_job(
        self,
        *,
        job_id: str,
        error_code: str,
        error_message: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                update jobs
                set
                  status = ?,
                  started_at = null,
                  completed_at = null,
                  lease_expires_at = null,
                  error_code = ?,
                  error_message = ?
                where job_id = ?
                """,
                (JOB_STATUS_QUEUED, error_code, error_message, job_id),
            )

    def fail_job(
        self,
        *,
        job_id: str,
        error_code: str,
        error_message: str,
        processing_duration_ms: float | None = None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                update jobs
                set
                  status = ?,
                  completed_at = ?,
                  lease_expires_at = null,
                  error_code = ?,
                  error_message = ?,
                  processing_duration_ms = coalesce(?, processing_duration_ms)
                where job_id = ?
                """,
                (
                    JOB_STATUS_FAILED,
                    time.time(),
                    error_code,
                    error_message,
                    processing_duration_ms,
                    job_id,
                ),
            )

    def heartbeat_worker(self, worker_id: str, *, current_job_id: str | None, status: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                insert into worker_heartbeats (worker_id, heartbeat_at, current_job_id, status)
                values (?, ?, ?, ?)
                on conflict(worker_id) do update set
                  heartbeat_at = excluded.heartbeat_at,
                  current_job_id = excluded.current_job_id,
                  status = excluded.status
                """,
                (worker_id, time.time(), current_job_id, status),
            )

    def delete_worker(self, worker_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                delete from worker_heartbeats
                where worker_id = ?
                """,
                (worker_id,),
            )

    def get_health_snapshot(self) -> QueueHealthSnapshot:
        now = time.time()
        with self._connection() as connection:
            snapshot = self._read_queue_snapshot(connection)
            oldest_row = connection.execute(
                """
                select min(created_at) as oldest_created_at
                from jobs
                where status = ?
                """,
                (JOB_STATUS_QUEUED,),
            ).fetchone()
            heartbeat = self._read_worker_heartbeat_snapshot(connection, now)

        oldest_queued_age_seconds = None
        if oldest_row is not None and oldest_row["oldest_created_at"] is not None:
            oldest_queued_age_seconds = max(0.0, now - float(oldest_row["oldest_created_at"]))

        worker_heartbeat_fresh = heartbeat.fresh_worker_count >= max(1, self.config.worker_concurrency)
        return QueueHealthSnapshot(
            queued_jobs=snapshot.queued_jobs,
            processing_jobs=snapshot.processing_jobs,
            oldest_queued_age_seconds=oldest_queued_age_seconds,
            average_processing_seconds=snapshot.average_processing_seconds,
            fresh_worker_count=heartbeat.fresh_worker_count,
            total_worker_rows=heartbeat.total_worker_rows,
            most_recent_heartbeat_age_seconds=heartbeat.most_recent_heartbeat_age_seconds,
            worker_heartbeat_fresh=worker_heartbeat_fresh,
        )

    def _mark_expired_jobs_failed(self, connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            """
            update jobs
            set
              status = ?,
              completed_at = ?,
              lease_expires_at = null,
              error_code = coalesce(error_code, 'retry_exhausted'),
              error_message = coalesce(error_message, 'Job exceeded the retry limit.')
            where
              status = ?
              and lease_expires_at is not null
              and lease_expires_at <= ?
              and attempt_count >= ?
            """,
            (
                JOB_STATUS_FAILED,
                now,
                JOB_STATUS_PROCESSING,
                now,
                self.config.total_allowed_attempts,
            ),
        )

    def _read_queue_snapshot(self, connection: sqlite3.Connection) -> QueueSnapshot:
        counts_row = connection.execute(
            """
            select
              sum(case when status = ? then 1 else 0 end) as queued_jobs,
              sum(case when status = ? then 1 else 0 end) as processing_jobs
            from jobs
            """,
            (JOB_STATUS_QUEUED, JOB_STATUS_PROCESSING),
        ).fetchone()
        duration_row = connection.execute(
            f"""
            select avg(processing_duration_ms) as avg_duration_ms
            from (
              select processing_duration_ms
              from jobs
              where status = ? and processing_duration_ms is not null
              order by completed_at desc
              limit {self.config.processing_average_window}
            )
            """,
            (JOB_STATUS_COMPLETED,),
        ).fetchone()
        average_processing_seconds = self.config.default_processing_seconds
        if duration_row is not None and duration_row["avg_duration_ms"] is not None:
            average_processing_seconds = max(
                0.1,
                float(duration_row["avg_duration_ms"]) / 1000.0,
            )
        return QueueSnapshot(
            queued_jobs=int(counts_row["queued_jobs"] or 0),
            processing_jobs=int(counts_row["processing_jobs"] or 0),
            average_processing_seconds=average_processing_seconds,
        )

    def _read_worker_heartbeat_snapshot(
        self,
        connection: sqlite3.Connection,
        now: float,
    ) -> WorkerHeartbeatSnapshot:
        cutoff = now - self.config.worker_heartbeat_timeout_seconds
        row = connection.execute(
            """
            select
              sum(case when heartbeat_at >= ? then 1 else 0 end) as fresh_worker_count,
              count(*) as total_worker_rows,
              max(heartbeat_at) as last_heartbeat_at
            from worker_heartbeats
            """,
            (cutoff,),
        ).fetchone()
        last_heartbeat_at = None if row is None else row["last_heartbeat_at"]
        age = None
        if last_heartbeat_at is not None:
            age = max(0.0, now - float(last_heartbeat_at))
        return WorkerHeartbeatSnapshot(
            fresh_worker_count=int((row["fresh_worker_count"] if row is not None else 0) or 0),
            total_worker_rows=int((row["total_worker_rows"] if row is not None else 0) or 0),
            most_recent_heartbeat_age_seconds=age,
        )

    def _estimate_completion_seconds(
        self,
        *,
        queued_jobs: int,
        processing_jobs: int,
        average_processing_seconds: float,
        include_new_job: bool,
    ) -> int:
        items = max(0, queued_jobs) + max(0, processing_jobs)
        if include_new_job:
            items += 1
        workers = max(1, self.config.worker_concurrency)
        return int(math.ceil((items / workers) * average_processing_seconds))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.config.jobs_db_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("pragma journal_mode = wal")
        connection.execute("pragma synchronous = normal")
        connection.execute("pragma busy_timeout = 30000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _row_to_job(self, row: sqlite3.Row | None) -> JobRecord | None:
        if row is None:
            return None
        result_json = None
        if row["result_json"]:
            result_json = json.loads(str(row["result_json"]))
        return JobRecord(
            row_id=int(row["row_id"]),
            job_id=str(row["job_id"]),
            request_id=str(row["request_id"]),
            status=str(row["status"]),
            created_at=float(row["created_at"]),
            started_at=None if row["started_at"] is None else float(row["started_at"]),
            completed_at=None if row["completed_at"] is None else float(row["completed_at"]),
            lease_expires_at=None if row["lease_expires_at"] is None else float(row["lease_expires_at"]),
            attempt_count=int(row["attempt_count"]),
            filename=None if row["filename"] is None else str(row["filename"]),
            include_debug=bool(row["include_debug"]),
            payload_path=None if row["payload_path"] is None else str(row["payload_path"]),
            payload_size_bytes=int(row["payload_size_bytes"]),
            result_json=result_json,
            error_code=None if row["error_code"] is None else str(row["error_code"]),
            error_message=None if row["error_message"] is None else str(row["error_message"]),
            processing_duration_ms=None
            if row["processing_duration_ms"] is None
            else float(row["processing_duration_ms"]),
        )


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _non_negative_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = int(raw)
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer.")
    return value


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive number.")
    return value
