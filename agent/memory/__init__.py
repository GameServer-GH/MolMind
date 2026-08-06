"""PostgreSQL-backed agent memory (sessions, events, artifacts)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.memory.attachments import (
    ALLOWED_ATTACHMENT_EXTENSIONS,
    attachment_kind_for_filename,
    format_attachment_context,
    guess_media_type,
    is_allowed_attachment_filename,
    summarize_attachment_for_context,
)
from agent.memory.blob_store import LocalBlobStore, S3BlobStore, build_blob_store
from agent.memory.factory import build_job_store, build_store, database_url, redis_url
from agent.memory.jobs_store import BackgroundJobStore
from agent.memory.models import AgentSession, Artifact, _now, default_runs_root
from agent.memory.postgres_store import PostgresRunStore

__all__ = [
    "ALLOWED_ATTACHMENT_EXTENSIONS",
    "AgentSession",
    "Artifact",
    "BackgroundJobStore",
    "FileRunStore",
    "LocalBlobStore",
    "PostgresRunStore",
    "S3BlobStore",
    "STORE",
    "attachment_kind_for_filename",
    "build_blob_store",
    "build_job_store",
    "build_store",
    "database_url",
    "default_runs_root",
    "format_attachment_context",
    "get_store",
    "guess_media_type",
    "is_allowed_attachment_filename",
    "redis_url",
    "summarize_attachment_for_context",
]

_STORE: PostgresRunStore | None = None


def get_store() -> PostgresRunStore:
    global _STORE
    if _STORE is None:
        _STORE = build_store()
    return _STORE


class _StoreProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(get_store(), name)


STORE = _StoreProxy()


def FileRunStore(*, root: Path | None = None, **kwargs: Any) -> PostgresRunStore:
    """Deprecated name: returns PostgresRunStore with namespace isolation per root."""
    namespace = str(root.resolve()) if root is not None else ""
    return build_store(blob_root=root, namespace=namespace, **kwargs)
