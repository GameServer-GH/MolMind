"""PostgreSQL-backed background job store with leased crash recovery."""

from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


def default_lease_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def default_lease_seconds() -> int:
    return max(30, int(os.environ.get("MOLMIND_JOB_LEASE_SECONDS") or 120))


def default_max_attempts() -> int:
    return max(1, int(os.environ.get("MOLMIND_JOB_MAX_ATTEMPTS") or 5))


class BackgroundJobStore:
    def __init__(self, *, dsn: str) -> None:
        if not dsn.strip():
            raise RuntimeError("BackgroundJobStore requires a PostgreSQL DSN")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL 存储需要安装 psycopg[binary]") from exc
        self._psycopg = psycopg
        self._dict_row = dict_row
        self.dsn = dsn
        from agent.memory.schema import ensure_schema

        with self._connect() as connection:
            ensure_schema(connection)

    def _connect(self):
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

    def upsert(self, job: dict) -> None:
        now = _now()
        from psycopg.types.json import Json

        finished = job.get("status") in {
            "ready",
            "completed",
            "failed",
            "error",
            "cancelled",
        }
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_background_jobs (
                    job_id, kind, session_id, run_id, status, progress, result_ref,
                    error, cancel_reason, payload, created_at, updated_at, finished_at,
                    lease_owner, lease_until, attempt
                ) VALUES (
                    %(job_id)s, %(kind)s, %(session_id)s, %(run_id)s, %(status)s,
                    %(progress)s, %(result_ref)s, %(error)s, %(cancel_reason)s,
                    %(payload)s, %(created_at)s, %(updated_at)s, %(finished_at)s,
                    %(lease_owner)s, %(lease_until)s, %(attempt)s
                )
                ON CONFLICT (job_id) DO UPDATE SET
                    kind=EXCLUDED.kind,
                    session_id=EXCLUDED.session_id,
                    run_id=EXCLUDED.run_id,
                    status=EXCLUDED.status,
                    progress=EXCLUDED.progress,
                    result_ref=EXCLUDED.result_ref,
                    error=EXCLUDED.error,
                    cancel_reason=EXCLUDED.cancel_reason,
                    payload=EXCLUDED.payload,
                    updated_at=EXCLUDED.updated_at,
                    finished_at=EXCLUDED.finished_at,
                    lease_owner=CASE
                        WHEN %(clear_lease)s THEN ''
                        WHEN EXCLUDED.lease_owner <> '' THEN EXCLUDED.lease_owner
                        ELSE agent_background_jobs.lease_owner
                    END,
                    lease_until=CASE
                        WHEN %(clear_lease)s THEN NULL
                        WHEN EXCLUDED.lease_until IS NOT NULL THEN EXCLUDED.lease_until
                        ELSE agent_background_jobs.lease_until
                    END,
                    attempt=GREATEST(agent_background_jobs.attempt, EXCLUDED.attempt)
                """,
                {
                    "job_id": str(job.get("job_id") or ""),
                    "kind": str(job.get("kind") or ""),
                    "session_id": str(job.get("session_id") or ""),
                    "run_id": str(job.get("run_id") or ""),
                    "status": str(job.get("status") or "queued"),
                    "progress": Json(job.get("progress") or {}),
                    "result_ref": Json(job.get("result_ref") or {}),
                    "error": str(job.get("error") or ""),
                    "cancel_reason": str(job.get("cancel_reason") or ""),
                    "payload": Json(job.get("payload") or {}),
                    "created_at": job.get("created_at") or now,
                    "updated_at": job.get("updated_at") or now,
                    "finished_at": job.get("finished_at") or (now if finished else None),
                    "lease_owner": str(job.get("lease_owner") or ""),
                    "lease_until": job.get("lease_until"),
                    "attempt": int(job.get("attempt") or 0),
                    "clear_lease": bool(finished or job.get("clear_lease")),
                },
            )
            connection.commit()

    def get(self, job_id: str) -> dict | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM agent_background_jobs WHERE job_id = %s",
                (job_id,),
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def update_status(
        self,
        job_id: str,
        *,
        status: str,
        progress: dict | None = None,
        result_ref: dict | None = None,
        error: str = "",
        finished: bool = False,
    ) -> bool:
        now = _now()
        from psycopg.types.json import Json

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE agent_background_jobs SET
                    status = %s,
                    progress = COALESCE(%s, progress),
                    result_ref = COALESCE(%s, result_ref),
                    error = COALESCE(NULLIF(%s, ''), error),
                    updated_at = %s,
                    finished_at = CASE WHEN %s THEN %s ELSE finished_at END,
                    lease_owner = CASE WHEN %s THEN '' ELSE lease_owner END,
                    lease_until = CASE WHEN %s THEN NULL ELSE lease_until END
                WHERE job_id = %s
                """,
                (
                    status,
                    Json(progress) if progress is not None else None,
                    Json(result_ref) if result_ref is not None else None,
                    error,
                    now,
                    finished,
                    now if finished else None,
                    finished,
                    finished,
                    job_id,
                ),
            )
            updated = cursor.rowcount == 1
            connection.commit()
        return updated

    def acquire_lease(
        self,
        job_id: str,
        *,
        owner: str,
        lease_seconds: int | None = None,
        status: str | None = None,
    ) -> bool:
        now = _now()
        until = now + timedelta(seconds=lease_seconds or default_lease_seconds())
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE agent_background_jobs SET
                    lease_owner = %s,
                    lease_until = %s,
                    updated_at = %s,
                    status = COALESCE(%s, status)
                WHERE job_id = %s
                  AND status NOT IN ('ready', 'completed', 'failed', 'error', 'cancelled')
                  AND (
                    lease_owner = ''
                    OR lease_owner = %s
                    OR lease_until IS NULL
                    OR lease_until < %s
                  )
                """,
                (
                    owner,
                    until,
                    now,
                    status,
                    job_id,
                    owner,
                    now,
                ),
            )
            updated = cursor.rowcount == 1
            connection.commit()
        return updated

    def renew_lease(
        self,
        job_id: str,
        *,
        owner: str,
        lease_seconds: int | None = None,
    ) -> bool:
        now = _now()
        until = now + timedelta(seconds=lease_seconds or default_lease_seconds())
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE agent_background_jobs SET
                    lease_until = %s,
                    updated_at = %s
                WHERE job_id = %s
                  AND lease_owner = %s
                  AND status IN ('queued', 'pending', 'running', 'recovering')
                """,
                (until, now, job_id, owner),
            )
            updated = cursor.rowcount == 1
            connection.commit()
        return updated

    def release_lease(self, job_id: str, *, owner: str = "") -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE agent_background_jobs SET
                    lease_owner = '',
                    lease_until = NULL,
                    updated_at = %s
                WHERE job_id = %s
                  AND (%s = '' OR lease_owner = %s OR lease_owner = '')
                """,
                (_now(), job_id, owner, owner),
            )
            updated = cursor.rowcount == 1
            connection.commit()
        return updated

    def request_cancel(self, job_id: str, *, reason: str = "cancelled") -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE agent_background_jobs SET
                    cancel_reason = %s,
                    status = CASE
                        WHEN status IN ('queued', 'pending', 'recovering') THEN 'cancelled'
                        ELSE 'cancel_requested'
                    END,
                    updated_at = %s,
                    lease_owner = CASE
                        WHEN status IN ('queued', 'pending', 'recovering') THEN ''
                        ELSE lease_owner
                    END,
                    lease_until = CASE
                        WHEN status IN ('queued', 'pending', 'recovering') THEN NULL
                        ELSE lease_until
                    END,
                    finished_at = CASE
                        WHEN status IN ('queued', 'pending', 'recovering') THEN %s
                        ELSE finished_at
                    END
                WHERE job_id = %s AND status IN (
                    'queued', 'pending', 'running', 'cancel_requested', 'recovering'
                )
                """,
                (str(reason or "cancelled"), _now(), _now(), job_id),
            )
            updated = cursor.rowcount == 1
            connection.commit()
        return updated

    def list_by_session(self, session_id: str, *, limit: int = 50) -> list[dict]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM agent_background_jobs
                WHERE session_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (session_id, max(1, limit)),
            )
            rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def list_live(self, *, limit: int = 100) -> list[dict]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM agent_background_jobs
                WHERE status IN ('queued', 'pending', 'running', 'recovering')
                ORDER BY created_at
                LIMIT %s
                """,
                (max(1, limit),),
            )
            rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def claim_stale(
        self,
        *,
        kinds: list[str] | None = None,
        stale_seconds: int | None = None,
        lease_seconds: int | None = None,
        max_attempts: int | None = None,
        owner: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Atomically claim orphaned jobs whose lease expired (or never held)."""
        now = _now()
        stale = max(5, int(stale_seconds or default_lease_seconds()))
        cutoff = now - timedelta(seconds=stale)
        lease_for = lease_seconds or default_lease_seconds()
        until = now + timedelta(seconds=lease_for)
        attempts_cap = max_attempts or default_max_attempts()
        claim_owner = owner or default_lease_owner()
        kind_filter = [str(item) for item in (kinds or []) if str(item)]

        with self._connect() as connection, connection.cursor() as cursor:
            # Exhausted retries → terminal failure (not reclaimed).
            if kind_filter:
                cursor.execute(
                    """
                    UPDATE agent_background_jobs SET
                        status = 'error',
                        error = COALESCE(NULLIF(error, ''), 'orphaned_max_attempts'),
                        updated_at = %s,
                        finished_at = %s,
                        lease_owner = '',
                        lease_until = NULL
                    WHERE status IN ('queued', 'pending', 'running', 'recovering')
                      AND attempt >= %s
                      AND kind = ANY(%s)
                      AND (lease_until IS NULL OR lease_until < %s OR updated_at < %s)
                    """,
                    (now, now, attempts_cap, kind_filter, now, cutoff),
                )
            else:
                cursor.execute(
                    """
                    UPDATE agent_background_jobs SET
                        status = 'error',
                        error = COALESCE(NULLIF(error, ''), 'orphaned_max_attempts'),
                        updated_at = %s,
                        finished_at = %s,
                        lease_owner = '',
                        lease_until = NULL
                    WHERE status IN ('queued', 'pending', 'running', 'recovering')
                      AND attempt >= %s
                      AND (lease_until IS NULL OR lease_until < %s OR updated_at < %s)
                    """,
                    (now, now, attempts_cap, now, cutoff),
                )

            params: list[Any]
            if kind_filter:
                sql = """
                    WITH stale AS (
                        SELECT job_id
                        FROM agent_background_jobs
                        WHERE status IN ('queued', 'pending', 'running', 'recovering')
                          AND attempt < %s
                          AND kind = ANY(%s)
                          AND (
                            lease_until IS NULL
                            OR lease_until < %s
                            OR (lease_owner = '' AND updated_at < %s)
                          )
                        ORDER BY updated_at
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE agent_background_jobs AS jobs
                    SET
                        status = 'recovering',
                        attempt = attempt + 1,
                        lease_owner = %s,
                        lease_until = %s,
                        updated_at = %s,
                        error = COALESCE(NULLIF(error, ''), 'orphaned_after_crash')
                    FROM stale
                    WHERE jobs.job_id = stale.job_id
                    RETURNING jobs.*
                """
                params = [
                    attempts_cap,
                    kind_filter,
                    now,
                    cutoff,
                    max(1, int(limit)),
                    claim_owner,
                    until,
                    now,
                ]
            else:
                sql = """
                    WITH stale AS (
                        SELECT job_id
                        FROM agent_background_jobs
                        WHERE status IN ('queued', 'pending', 'running', 'recovering')
                          AND attempt < %s
                          AND (
                            lease_until IS NULL
                            OR lease_until < %s
                            OR (lease_owner = '' AND updated_at < %s)
                          )
                        ORDER BY updated_at
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE agent_background_jobs AS jobs
                    SET
                        status = 'recovering',
                        attempt = attempt + 1,
                        lease_owner = %s,
                        lease_until = %s,
                        updated_at = %s,
                        error = COALESCE(NULLIF(error, ''), 'orphaned_after_crash')
                    FROM stale
                    WHERE jobs.job_id = stale.job_id
                    RETURNING jobs.*
                """
                params = [
                    attempts_cap,
                    now,
                    cutoff,
                    max(1, int(limit)),
                    claim_owner,
                    until,
                    now,
                ]
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            connection.commit()
        return [dict(row) for row in rows]
