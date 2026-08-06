"""Shared helpers for AgentRuntime unit tests without Postgres FileRunStore."""

from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace
from typing import Any

from agent.memory.attachments import attachment_kind_for_filename
from agent.memory.models import AgentSession
from agent.registry import get_registry
from agent.runtime.loop import AgentRuntime
from agent.runtime.task_router import TaskRouter


class MemRunStore:
    """In-memory store stub sufficient for most AgentRuntime unit paths."""

    def __init__(self) -> None:
        self._sessions: dict[str, AgentSession] = {}
        self._artifacts: dict[tuple[str, str], Any] = {}
        self._events: list[dict[str, Any]] = []
        self._blobs: dict[str, bytes] = {}

    def persist(self, session: AgentSession) -> None:
        self._sessions[str(session.session_id)] = session

    def create(self, *, profile_id: str = "competition_masld", client_id: str = "") -> AgentSession:
        session = AgentSession(
            session_id=f"mem-{len(self._sessions) + 1:04d}",
            client_id=client_id,
            profile_id=profile_id,
        )
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> AgentSession | None:
        return self._sessions.get(str(session_id))

    def set_title(self, session: AgentSession, text: str) -> None:
        return None

    def append_event(self, session: AgentSession, event: dict[str, Any]) -> dict[str, Any]:
        self._events.append(dict(event))
        return event

    def save_sdf(self, session: AgentSession) -> None:
        return None

    def put_artifact(self, session: AgentSession, artifact: Any) -> Any:
        key = (str(session.session_id), str(getattr(artifact, "artifact_id", "") or ""))
        self._artifacts[key] = artifact
        return artifact

    def get_artifact(self, session_id: str, artifact_id: str) -> Any | None:
        return self._artifacts.get((str(session_id), str(artifact_id)))

    def mutation_lock(self, session_id: str):
        from contextlib import nullcontext

        return nullcontext()

    def stage_attachment(
        self,
        session: AgentSession,
        *,
        filename: str,
        content: bytes,
        media_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        attachment_id = f"att-{uuid.uuid4().hex[:12]}"
        blob_id = f"blob-{uuid.uuid4().hex[:12]}"
        self._blobs[blob_id] = content
        metadata = {
            "attachment_id": attachment_id,
            "filename": filename or "attachment.bin",
            "media_type": media_type,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "state": "draft",
            "blob_id": blob_id,
            "kind": attachment_kind_for_filename(filename),
        }
        session.staged_attachments[attachment_id] = metadata
        self.persist(session)
        return dict(metadata)

    def read_staged_attachment(
        self, session: AgentSession, attachment_id: str
    ) -> tuple[dict[str, Any], bytes] | None:
        meta = session.staged_attachments.get(str(attachment_id))
        if not isinstance(meta, dict):
            return None
        content = self._blobs.get(str(meta.get("blob_id") or ""))
        if content is None:
            return None
        return dict(meta), content

    def delete_staged_attachment(self, session: AgentSession, attachment_id: str) -> bool:
        meta = session.staged_attachments.pop(str(attachment_id), None)
        if not isinstance(meta, dict):
            return False
        self._blobs.pop(str(meta.get("blob_id") or ""), None)
        self.persist(session)
        return True

def make_runtime(*, store: MemRunStore | None = None) -> AgentRuntime:
    """Construct a fully initialized AgentRuntime with an in-memory store."""
    return AgentRuntime(store=store or MemRunStore())


def make_runtime_stub() -> AgentRuntime:
    """Lightweight stub (no __init__); for pure method tests."""
    rt = AgentRuntime.__new__(AgentRuntime)
    rt.store = MemRunStore()
    rt.registry = get_registry()
    rt.task_router = TaskRouter(rt.registry)
    return rt


def make_session(**kwargs: Any) -> SimpleNamespace:
    base = {
        "messages": [],
        "last_result": None,
        "pending_goal": None,
        "pending_action": None,
        "run_history": [],
        "top_n": 10,
        "active_run": None,
        "sdf_bytes": None,
        "sdf_filename": "",
        "session_id": "mem-session",
        "installed_scp_skills": {},
        "working_memory": [],
    }
    base.update(kwargs)
    return SimpleNamespace(**base)
