"""Durable leased Run queue with SQLite and PostgreSQL backends."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


@dataclass(frozen=True)
class RunJob:
    job_id: str
    run_id: str
    session_id: str
    payload: dict[str, Any]
    attempt: int
    lease_owner: str
    lease_until: str


class RunQueue(Protocol):
    def enqueue(self, *, run_id: str, session_id: str, payload: dict[str, Any]) -> str: ...
    def claim(self, *, owner: str, lease_seconds: int) -> RunJob | None: ...
    def renew(self, job_id: str, *, owner: str, lease_seconds: int) -> bool: ...
    def complete(self, job_id: str, *, owner: str) -> bool: ...
    def fail(self, job_id: str, *, owner: str, error: str, max_attempts: int = 3) -> bool: ...
    def has_live_run(self, run_id: str) -> bool: ...
    def request_cancel(self, run_id: str, *, reason: str) -> bool: ...
    def cancel_reason(self, run_id: str) -> str: ...
    def close(self) -> None: ...


class SQLiteRunQueue:
    """Multi-process durable queue for one host/shared POSIX volume."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_run_jobs (
                    job_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_until TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    cancel_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_run_jobs_claim "
                "ON agent_run_jobs(status, available_at, lease_until, created_at)"
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_run_jobs)")}
            if "cancel_reason" not in columns:
                connection.execute(
                    "ALTER TABLE agent_run_jobs ADD COLUMN cancel_reason TEXT NOT NULL DEFAULT ''"
                )

    def enqueue(self, *, run_id: str, session_id: str, payload: dict[str, Any]) -> str:
        job_id = f"job-{uuid.uuid4().hex[:16]}"
        now = _iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_run_jobs
                    (job_id, run_id, session_id, payload, status, available_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (job_id, run_id, session_id, json.dumps(payload, ensure_ascii=False), now, now, now),
            )
            row = connection.execute(
                "SELECT job_id FROM agent_run_jobs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return str(row["job_id"])

    def claim(self, *, owner: str, lease_seconds: int = 60) -> RunJob | None:
        now = _now()
        now_text = _iso(now)
        lease_until = _iso(now + timedelta(seconds=max(5, lease_seconds)))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM agent_run_jobs
                WHERE (status = 'queued' AND available_at <= ?)
                   OR (status = 'leased' AND lease_until < ?)
                ORDER BY created_at, job_id
                LIMIT 1
                """,
                (now_text, now_text),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            attempt = int(row["attempt"] or 0) + 1
            connection.execute(
                """
                UPDATE agent_run_jobs
                SET status='leased', attempt=?, lease_owner=?, lease_until=?, updated_at=?
                WHERE job_id=?
                """,
                (attempt, owner, lease_until, now_text, row["job_id"]),
            )
            connection.execute("COMMIT")
            return RunJob(
                job_id=str(row["job_id"]),
                run_id=str(row["run_id"]),
                session_id=str(row["session_id"]),
                payload=json.loads(row["payload"] or "{}"),
                attempt=attempt,
                lease_owner=owner,
                lease_until=lease_until,
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def renew(self, job_id: str, *, owner: str, lease_seconds: int = 60) -> bool:
        until = _iso(_now() + timedelta(seconds=max(5, lease_seconds)))
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE agent_run_jobs SET lease_until=?, updated_at=? "
                "WHERE job_id=? AND status='leased' AND lease_owner=?",
                (until, _iso(), job_id, owner),
            )
            return cursor.rowcount == 1

    def complete(self, job_id: str, *, owner: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE agent_run_jobs SET status='completed', lease_owner='', lease_until='', updated_at=? "
                "WHERE job_id=? AND status='leased' AND lease_owner=?",
                (_iso(), job_id, owner),
            )
            return cursor.rowcount == 1

    def fail(self, job_id: str, *, owner: str, error: str, max_attempts: int = 3) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempt FROM agent_run_jobs WHERE job_id=? AND status='leased' AND lease_owner=?",
                (job_id, owner),
            ).fetchone()
            if row is None:
                return False
            attempt = int(row["attempt"] or 0)
            terminal = attempt >= max(1, max_attempts)
            available = _iso(_now() + timedelta(seconds=min(30, 2 ** max(0, attempt - 1))))
            connection.execute(
                """
                UPDATE agent_run_jobs
                SET status=?, available_at=?, lease_owner='', lease_until='', last_error=?, updated_at=?
                WHERE job_id=? AND lease_owner=?
                """,
                ("failed" if terminal else "queued", available, str(error)[:2000], _iso(), job_id, owner),
            )
            return True

    def has_live_run(self, run_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM agent_run_jobs WHERE run_id=? AND status IN ('queued','leased')",
                (run_id,),
            ).fetchone()
            return row is not None

    def request_cancel(self, run_id: str, *, reason: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE agent_run_jobs SET cancel_reason=?, updated_at=? "
                "WHERE run_id=? AND status IN ('queued','leased')",
                (str(reason or "cancelled"), _iso(), run_id),
            )
            return cursor.rowcount == 1

    def cancel_reason(self, run_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cancel_reason FROM agent_run_jobs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            return str(row["cancel_reason"] or "") if row else ""

    def close(self) -> None:
        return None


class PostgresRunQueue:
    """PostgreSQL queue using SKIP LOCKED for multi-host workers."""

    def __init__(self, dsn: str) -> None:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - depends on deployment extra
            raise RuntimeError("PostgreSQL 队列需要安装 psycopg[binary]") from exc
        self._psycopg = psycopg
        self.dsn = dsn
        self._init_schema()

    def _connect(self):
        return self._psycopg.connect(self.dsn)

    def _init_schema(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_run_jobs (
                    job_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    available_at TIMESTAMPTZ NOT NULL,
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_until TIMESTAMPTZ,
                    last_error TEXT NOT NULL DEFAULT '',
                    cancel_reason TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_run_jobs_claim "
                "ON agent_run_jobs(status, available_at, lease_until, created_at)"
            )
            cursor.execute(
                "ALTER TABLE agent_run_jobs ADD COLUMN IF NOT EXISTS "
                "cancel_reason TEXT NOT NULL DEFAULT ''"
            )

    def enqueue(self, *, run_id: str, session_id: str, payload: dict[str, Any]) -> str:
        job_id = f"job-{uuid.uuid4().hex[:16]}"
        now = _now()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_run_jobs
                    (job_id, run_id, session_id, payload, status, available_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, 'queued', %s, %s, %s)
                ON CONFLICT(run_id) DO UPDATE SET updated_at=agent_run_jobs.updated_at
                RETURNING job_id
                """,
                (job_id, run_id, session_id, json.dumps(payload, ensure_ascii=False), now, now, now),
            )
            return str(cursor.fetchone()[0])

    def claim(self, *, owner: str, lease_seconds: int = 60) -> RunJob | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH candidate AS (
                    SELECT job_id FROM agent_run_jobs
                    WHERE (status='queued' AND available_at <= NOW())
                       OR (status='leased' AND lease_until < NOW())
                    ORDER BY created_at, job_id
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE agent_run_jobs AS jobs
                SET status='leased', attempt=jobs.attempt+1, lease_owner=%s,
                    lease_until=NOW() + (%s * INTERVAL '1 second'), updated_at=NOW()
                FROM candidate WHERE jobs.job_id=candidate.job_id
                RETURNING jobs.job_id, jobs.run_id, jobs.session_id, jobs.payload,
                          jobs.attempt, jobs.lease_until
                """,
                (owner, max(5, lease_seconds)),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            payload = row[3] if isinstance(row[3], dict) else json.loads(row[3])
            return RunJob(str(row[0]), str(row[1]), str(row[2]), payload, int(row[4]), owner, row[5].isoformat())

    def renew(self, job_id: str, *, owner: str, lease_seconds: int = 60) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE agent_run_jobs SET lease_until=NOW()+(%s*INTERVAL '1 second'), updated_at=NOW() "
                "WHERE job_id=%s AND status='leased' AND lease_owner=%s",
                (max(5, lease_seconds), job_id, owner),
            )
            return cursor.rowcount == 1

    def complete(self, job_id: str, *, owner: str) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE agent_run_jobs SET status='completed', lease_owner='', lease_until=NULL, updated_at=NOW() "
                "WHERE job_id=%s AND status='leased' AND lease_owner=%s",
                (job_id, owner),
            )
            return cursor.rowcount == 1

    def fail(self, job_id: str, *, owner: str, error: str, max_attempts: int = 3) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE agent_run_jobs SET
                    status=CASE WHEN attempt >= %s THEN 'failed' ELSE 'queued' END,
                    available_at=NOW()+(LEAST(30, POWER(2, GREATEST(0, attempt-1))) * INTERVAL '1 second'),
                    lease_owner='', lease_until=NULL, last_error=%s, updated_at=NOW()
                WHERE job_id=%s AND status='leased' AND lease_owner=%s
                """,
                (max(1, max_attempts), str(error)[:2000], job_id, owner),
            )
            return cursor.rowcount == 1

    def has_live_run(self, run_id: str) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM agent_run_jobs WHERE run_id=%s AND status IN ('queued','leased')",
                (run_id,),
            )
            return cursor.fetchone() is not None

    def request_cancel(self, run_id: str, *, reason: str) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE agent_run_jobs SET cancel_reason=%s, updated_at=NOW() "
                "WHERE run_id=%s AND status IN ('queued','leased')",
                (str(reason or "cancelled"), run_id),
            )
            return cursor.rowcount == 1

    def cancel_reason(self, run_id: str) -> str:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT cancel_reason FROM agent_run_jobs WHERE run_id=%s", (run_id,))
            row = cursor.fetchone()
            return str(row[0] or "") if row else ""

    def close(self) -> None:
        return None


def build_run_queue(*, runs_root: Path) -> RunQueue:
    """Build the durable Run queue. Production and local both require PostgreSQL."""
    from agent.memory.factory import database_url

    url = str(os.environ.get("MOLMIND_AGENT_QUEUE_URL") or database_url() or "").strip()
    if url.startswith("postgresql://") or url.startswith("postgres://"):
        return PostgresRunQueue(url)
    raise RuntimeError(
        "Agent run queue requires PostgreSQL. Set MOLMIND_DATABASE_URL or "
        "MOLMIND_AGENT_QUEUE_URL to a postgresql:// DSN. "
        f"(sqlite and file backends are disabled; got {url[:32]!r})"
    )


class RunQueueWorkers:
    """Lease-renewing worker pool shared by API and dedicated worker entrypoints."""

    def __init__(
        self,
        queue: RunQueue,
        handler: Callable[[RunJob], None],
        cancel_handler: Callable[[RunJob, str], None] | None = None,
        *,
        workers: int = 2,
        lease_seconds: int = 60,
        poll_seconds: float = 0.25,
    ) -> None:
        self.queue = queue
        self.handler = handler
        self.cancel_handler = cancel_handler
        self.worker_count = max(1, int(workers))
        self.lease_seconds = max(15, int(lease_seconds))
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.owner_prefix = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        if self.threads:
            return
        for index in range(self.worker_count):
            thread = threading.Thread(
                target=self._loop,
                args=(f"{self.owner_prefix}-{index}",),
                daemon=True,
                name=f"agent-run-worker-{index}",
            )
            thread.start()
            self.threads.append(thread)

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=timeout)
        self.threads.clear()
        self.queue.close()

    def _loop(self, owner: str) -> None:
        while not self.stop_event.is_set():
            try:
                job = self.queue.claim(owner=owner, lease_seconds=self.lease_seconds)
            except Exception:
                self.stop_event.wait(self.poll_seconds)
                continue
            if job is None:
                self.stop_event.wait(self.poll_seconds)
                continue
            renew_stop = threading.Event()
            renewer = threading.Thread(
                target=self._renew_loop,
                args=(job, owner, renew_stop),
                daemon=True,
                name=f"agent-run-renew-{job.job_id[-6:]}",
            )
            renewer.start()
            try:
                self.handler(job)
            except Exception as exc:  # noqa: BLE001 - queue boundary
                self.queue.fail(job.job_id, owner=owner, error=str(exc))
            else:
                self.queue.complete(job.job_id, owner=owner)
            finally:
                renew_stop.set()
                renewer.join(timeout=1)

    def _renew_loop(self, job: RunJob, owner: str, stop: threading.Event) -> None:
        interval = max(5.0, self.lease_seconds / 3)
        next_renew = time.monotonic() + interval
        while not stop.wait(0.5):
            reason = self.queue.cancel_reason(job.run_id)
            if reason and self.cancel_handler is not None:
                self.cancel_handler(job, reason)
                return
            if time.monotonic() < next_renew:
                continue
            if not self.queue.renew(
                job.job_id,
                owner=owner,
                lease_seconds=self.lease_seconds,
            ):
                return
            next_renew = time.monotonic() + interval
