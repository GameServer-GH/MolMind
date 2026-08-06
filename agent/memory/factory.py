"""Factory helpers for PostgreSQL-backed agent memory."""

from __future__ import annotations

import os
from pathlib import Path

from agent.memory.blob_store import BlobStore, LocalBlobStore, build_blob_store
from agent.memory.jobs_store import BackgroundJobStore
from agent.memory.models import default_runs_root
from agent.memory.postgres_store import PostgresRunStore

_DEFAULT_DSN = "postgresql://molmind:molmind@127.0.0.1:15432/molmind"
_DEFAULT_REDIS = "redis://127.0.0.1:6379/0"


def database_url() -> str:
    for key in ("MOLMIND_DATABASE_URL", "MOLMIND_AGENT_QUEUE_URL"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return _DEFAULT_DSN


def redis_url() -> str | None:
    explicit = str(os.environ.get("MOLMIND_REDIS_URL") or "").strip()
    if explicit:
        return explicit
    if os.environ.get("MOLMIND_REDIS_URL") == "":
        return None
    return _DEFAULT_REDIS


def build_store(
    *,
    blob_root: Path | None = None,
    dsn: str | None = None,
    redis_url_override: str | None = None,
    namespace: str = "",
    blob_store: BlobStore | None = None,
) -> PostgresRunStore:
    resolved_dsn = (dsn or database_url()).strip()
    if not resolved_dsn:
        raise RuntimeError(
            "PostgreSQL is required for agent memory. Set MOLMIND_DATABASE_URL "
            "or MOLMIND_AGENT_QUEUE_URL."
        )
    env_blob = str(os.environ.get("MOLMIND_BLOB_ROOT") or "").strip()
    root = blob_root or (Path(env_blob) if env_blob else (default_runs_root() / "blobs"))
    resolved_redis = redis_url_override if redis_url_override is not None else redis_url()
    store = blob_store
    if store is None:
        if str(os.environ.get("MOLMIND_BLOB_STORE_URL") or "").strip():
            store = build_blob_store(blob_root=root)
        else:
            store = LocalBlobStore(root)
    return PostgresRunStore(
        dsn=resolved_dsn,
        blob_root=root,
        blob_store=store,
        redis_url=resolved_redis,
        namespace=namespace,
    )


def build_job_store(*, dsn: str | None = None) -> BackgroundJobStore:
    resolved_dsn = (dsn or database_url()).strip()
    if not resolved_dsn:
        raise RuntimeError(
            "PostgreSQL is required for background jobs. Set MOLMIND_DATABASE_URL."
        )
    return BackgroundJobStore(dsn=resolved_dsn)
