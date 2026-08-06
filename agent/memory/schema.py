"""PostgreSQL schema bootstrap for agent memory."""

from __future__ import annotations

from typing import Any


def ensure_schema(connection: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_clients (
                client_id TEXT PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_sessions (
                session_id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL DEFAULT '',
                namespace TEXT NOT NULL DEFAULT '',
                state JSONB NOT NULL,
                revision BIGINT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                deleted_at TIMESTAMPTZ NULL
            )
            """
        )
        cursor.execute(
            "ALTER TABLE agent_sessions ADD COLUMN IF NOT EXISTS namespace TEXT NOT NULL DEFAULT ''"
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_sessions_client_updated
            ON agent_sessions (client_id, updated_at DESC)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_sessions_active
            ON agent_sessions (deleted_at) WHERE deleted_at IS NULL
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_sessions_namespace
            ON agent_sessions (namespace) WHERE deleted_at IS NULL
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_events (
                session_id TEXT NOT NULL,
                seq BIGINT NOT NULL,
                payload JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (session_id, seq)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_blobs (
                blob_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL,
                media_type TEXT NOT NULL,
                byte_size BIGINT NOT NULL,
                content_sha256 TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_background_jobs (
                job_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                run_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                progress JSONB NOT NULL DEFAULT '{}',
                result_ref JSONB NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                cancel_reason TEXT NOT NULL DEFAULT '',
                payload JSONB NOT NULL DEFAULT '{}',
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                finished_at TIMESTAMPTZ NULL,
                lease_owner TEXT NOT NULL DEFAULT '',
                lease_until TIMESTAMPTZ,
                attempt INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        cursor.execute(
            "ALTER TABLE agent_background_jobs ADD COLUMN IF NOT EXISTS "
            "lease_owner TEXT NOT NULL DEFAULT ''"
        )
        cursor.execute(
            "ALTER TABLE agent_background_jobs ADD COLUMN IF NOT EXISTS "
            "lease_until TIMESTAMPTZ"
        )
        cursor.execute(
            "ALTER TABLE agent_background_jobs ADD COLUMN IF NOT EXISTS "
            "attempt INTEGER NOT NULL DEFAULT 0"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_background_jobs_lease "
            "ON agent_background_jobs(status, lease_until, updated_at)"
        )
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
    connection.commit()
