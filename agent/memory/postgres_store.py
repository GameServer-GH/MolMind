"""PostgreSQL-backed agent session store."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.memory.blob_store import BlobStore, LocalBlobStore
from agent.memory.models import AgentSession, Artifact, _now, default_runs_root
from agent.memory.redis_coord import RedisCoordinator
from agent.memory.schema import ensure_schema


class PostgresRunStore:
    lease_managed: bool = True

    def __init__(
        self,
        *,
        dsn: str,
        blob_root: Path | None = None,
        blob_store: BlobStore | None = None,
        redis_url: str | None = None,
        namespace: str = "",
    ) -> None:
        if not dsn.strip():
            raise RuntimeError("PostgresRunStore requires a PostgreSQL DSN")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL 存储需要安装 psycopg[binary]") from exc
        self._psycopg = psycopg
        self._dict_row = dict_row
        self.dsn = dsn
        self._blob_root = Path(blob_root or (default_runs_root() / "blobs"))
        if blob_store is None:
            self._blob_root.mkdir(parents=True, exist_ok=True)
            self._blobs: BlobStore = LocalBlobStore(self._blob_root)
        else:
            self._blobs = blob_store
            if isinstance(blob_store, LocalBlobStore):
                self._blob_root = Path(blob_store.root)
        self._redis = RedisCoordinator(redis_url)
        self.namespace = namespace or ""
        self._lock = threading.Lock()
        self._held_advisory = threading.local()
        self._sessions: dict[str, AgentSession] = {}
        self._session_updated: dict[str, str] = {}
        with self._connect() as connection:
            ensure_schema(connection)

    @property
    def root(self) -> Path:
        if self._blob_root.name == "blobs":
            return self._blob_root.parent
        return self._blob_root

    @property
    def blobs(self) -> BlobStore:
        return self._blobs

    def _connect(self):
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

    def _namespace_clause(self) -> tuple[str, list[Any]]:
        if self.namespace:
            return " AND namespace = %s", [self.namespace]
        return "", []

    @contextmanager
    def mutation_lock(self, session_id: str):
        """Cross-process short lock for one session's atomic state mutation.

        Prefer Redis. Fall back to PostgreSQL session-level advisory locks.
        Never hold a row FOR UPDATE across persist() — that deadlocks because
        persist uses a separate connection to UPDATE the same row.
        """
        use_redis = self._redis.enabled
        if use_redis:
            lock_cm = self._redis.mutation_lock(session_id, require=True)
        else:
            lock_cm = nullcontext()

        def _invalidate_if_stale() -> None:
            # Force the next get() to compare stamps; do not drop identity.
            with self._lock:
                self._session_updated.pop(session_id, None)

        with lock_cm:
            if not use_redis:
                held = getattr(self._held_advisory, "ids", None)
                if held is None:
                    self._held_advisory.ids = set()
                    held = self._held_advisory.ids
                if session_id in held:
                    _invalidate_if_stale()
                    yield
                    return
                conn = self._connect()
                try:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            "SELECT pg_advisory_lock(hashtext(%s))",
                            (session_id,),
                        )
                    held.add(session_id)
                    _invalidate_if_stale()
                    try:
                        yield
                    finally:
                        held.discard(session_id)
                        with conn.cursor() as cursor:
                            cursor.execute(
                                "SELECT pg_advisory_unlock(hashtext(%s))",
                                (session_id,),
                            )
                finally:
                    conn.close()
                return
            _invalidate_if_stale()
            yield

    def register_client(self, client_id: str) -> None:
        if not client_id:
            return
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_clients (client_id, created_at)
                VALUES (%s, NOW())
                ON CONFLICT (client_id) DO NOTHING
                """,
                (client_id,),
            )
            connection.commit()

    def client_exists(self, client_id: str) -> bool:
        if not client_id:
            return False
        ns_clause, ns_params = self._namespace_clause()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM agent_clients WHERE client_id = %s",
                (client_id,),
            )
            if cursor.fetchone():
                return True
            cursor.execute(
                f"""
                SELECT 1 FROM agent_sessions
                WHERE client_id = %s AND deleted_at IS NULL{ns_clause}
                LIMIT 1
                """,
                [client_id, *ns_params],
            )
            return cursor.fetchone() is not None

    def create(
        self,
        *,
        profile_id: str = "competition_masld",
        client_id: str = "",
    ) -> AgentSession:
        sid = uuid.uuid4().hex
        session = AgentSession(
            session_id=sid,
            profile_id=profile_id,
            client_id=client_id,
        )
        self.register_client(client_id)
        with self._lock:
            self._sessions[sid] = session
            self._session_updated[sid] = session.updated_at
        self._persist_meta(session)
        return session

    def get(self, session_id: str) -> AgentSession | None:
        stamp = self._fetch_updated_at(session_id)
        if stamp is None:
            with self._lock:
                self._sessions.pop(session_id, None)
                self._session_updated.pop(session_id, None)
            return None
        with self._lock:
            cached = self._sessions.get(session_id)
            if cached is not None and self._session_updated.get(session_id) == stamp:
                return cached
        loaded = self._load_session(session_id)
        if loaded is None:
            return None
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                self._copy_session_state(existing, loaded)
                self._session_updated[session_id] = stamp
                return existing
            self._sessions[session_id] = loaded
            self._session_updated[session_id] = stamp
            return loaded

    @staticmethod
    def _copy_session_state(target: AgentSession, source: AgentSession) -> None:
        """Refresh a cached session object in place to preserve identity."""
        # append_event may advance memory ahead of a stale meta snapshot that a
        # concurrent get() is about to load; never let that reload regress seq.
        # Likewise, never wipe a hot last_result with a DB reload that only has
        # the durable frozen_ranking summary (or neither).
        preserved_seq = int(getattr(target, "event_seq", 0) or 0)
        preserved_result = getattr(target, "last_result", None)
        preserved_freeze = getattr(target, "frozen_ranking", None)
        for field_name in source.__dataclass_fields__:  # type: ignore[attr-defined]
            if field_name == "session_id":
                continue
            setattr(target, field_name, getattr(source, field_name))
        target.event_seq = max(preserved_seq, int(getattr(target, "event_seq", 0) or 0))
        if preserved_result is not None and getattr(target, "last_result", None) is None:
            target.last_result = preserved_result
        if preserved_freeze and not getattr(target, "frozen_ranking", None):
            target.frozen_ranking = preserved_freeze

    def _fetch_updated_at(self, session_id: str) -> str | None:
        ns_clause, ns_params = self._namespace_clause()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT updated_at FROM agent_sessions
                WHERE session_id = %s AND deleted_at IS NULL{ns_clause}
                """,
                [session_id, *ns_params],
            )
            row = cursor.fetchone()
        if not row:
            return None
        value = row["updated_at"]
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    def _fetch_revision(self, session_id: str) -> int | None:
        ns_clause, ns_params = self._namespace_clause()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT revision FROM agent_sessions
                WHERE session_id = %s AND deleted_at IS NULL{ns_clause}
                """,
                [session_id, *ns_params],
            )
            row = cursor.fetchone()
        return int(row["revision"]) if row else None

    def list_sessions(
        self,
        *,
        limit: int = 50,
        client_id: str | None = None,
    ) -> list[dict[str, Any]]:
        ns_clause, ns_params = self._namespace_clause()
        params: list[Any] = []
        filters = "deleted_at IS NULL"
        if client_id is not None:
            filters += " AND client_id = %s"
            params.append(client_id)
        filters += ns_clause
        params.extend(ns_params)
        params.append(max(1, limit))
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT session_id, state, updated_at, created_at
                FROM agent_sessions
                WHERE {filters}
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                params,
            )
            rows = cursor.fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            meta = row["state"] if isinstance(row["state"], dict) else json.loads(row["state"])
            preview = ""
            for msg in reversed(meta.get("messages") or []):
                if msg.get("role") == "user" and msg.get("text"):
                    preview = str(msg["text"]).strip()
                    break
            title = str(meta.get("title") or "").strip() or preview[:48] or "未命名对话"
            items.append(
                {
                    "session_id": meta.get("session_id") or row["session_id"],
                    "title": title,
                    "preview": preview[:120],
                    "created_at": meta.get("created_at") or "",
                    "updated_at": meta.get("updated_at") or meta.get("created_at") or "",
                    "sdf_filename": meta.get("sdf_filename") or "",
                    "has_sdf": bool(meta.get("has_sdf")),
                    "profile_id": meta.get("profile_id") or "competition_masld",
                    "artifact_count": len(meta.get("artifact_ids") or []),
                    "event_seq": int(meta.get("event_seq") or 0),
                    "run_status": str((meta.get("active_run") or {}).get("status") or ""),
                    "active_run_id": (
                        str((meta.get("active_run") or {}).get("run_id") or "")
                        if str((meta.get("active_run") or {}).get("status") or "")
                        in {"queued", "running", "cancel_requested"}
                        else ""
                    ),
                }
            )
        return items

    def set_title(self, session: AgentSession, title: str) -> None:
        session.title = (title or "").strip()[:80]
        self._persist_meta(session)

    def delete_session(self, session_id: str) -> bool:
        ns_clause, ns_params = self._namespace_clause()
        with self._lock:
            self._sessions.pop(session_id, None)
            self._session_updated.pop(session_id, None)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT session_id FROM agent_sessions
                WHERE session_id = %s AND deleted_at IS NULL{ns_clause}
                """,
                [session_id, *ns_params],
            )
            if cursor.fetchone() is None:
                return False
            cursor.execute(
                "SELECT blob_id FROM agent_blobs WHERE session_id = %s",
                (session_id,),
            )
            blob_ids = [str(row["blob_id"]) for row in cursor.fetchall()]
            cursor.execute("DELETE FROM agent_events WHERE session_id = %s", (session_id,))
            cursor.execute("DELETE FROM agent_blobs WHERE session_id = %s", (session_id,))
            cursor.execute("DELETE FROM agent_sessions WHERE session_id = %s", (session_id,))
            connection.commit()
        for blob_id in blob_ids:
            self._blobs.delete(blob_id)
        return True

    def clear_sessions(self, *, client_id: str | None = None) -> int:
        ns_clause, ns_params = self._namespace_clause()
        params: list[Any] = []
        filters = "deleted_at IS NULL"
        if client_id is not None:
            filters += " AND client_id = %s"
            params.append(client_id)
        filters += ns_clause
        params.extend(ns_params)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT session_id FROM agent_sessions WHERE {filters}",
                params,
            )
            session_ids = [str(row["session_id"]) for row in cursor.fetchall()]
        removed = 0
        for session_id in session_ids:
            if self.delete_session(session_id):
                removed += 1
        return removed

    def put_artifact(self, session: AgentSession, artifact: Artifact) -> Artifact:
        blob = self._register_blob(
            session.session_id,
            artifact.content,
            kind="artifact",
            media_type=artifact.media_type,
        )
        with self._lock:
            session.artifacts[artifact.artifact_id] = artifact
        self._persist_meta(session, extra_blob_refs={artifact.artifact_id: blob["blob_id"]})
        return artifact

    def _max_event_seq(self, session_id: str, *, cursor=None) -> int:
        """Return the highest durable event seq for a session (0 if none)."""

        def _query(active_cursor) -> int:
            active_cursor.execute(
                """
                SELECT COALESCE(MAX(seq), 0) AS max_seq
                FROM agent_events
                WHERE session_id = %s
                """,
                (session_id,),
            )
            row = active_cursor.fetchone() or {}
            return int(row.get("max_seq") or 0)

        if cursor is not None:
            return _query(cursor)
        with self._connect() as connection, connection.cursor() as owned:
            return _query(owned)

    def append_event(self, session: AgentSession, event: dict[str, Any]) -> dict[str, Any]:
        """Append an event with a monotonically increasing seq.

        Seq allocation is based on max(memory, durable agent_events) so a
        concurrent get()/persist that reloads stale meta cannot reuse a seq.
        Unique violations are retried. The session meta event_seq is bumped in
        the same transaction so reloads observe the advance.
        """
        base = {key: value for key, value in event.items() if key != "seq"}
        base.setdefault("occurred_at", _now())
        unique_violation = getattr(getattr(self._psycopg, "errors", None), "UniqueViolation", None)
        last_error: Exception | None = None

        for _attempt in range(8):
            with self._connect() as connection, connection.cursor() as cursor:
                max_seq = self._max_event_seq(session.session_id, cursor=cursor)
                next_seq = max(int(session.event_seq or 0), max_seq) + 1
                payload = {"seq": next_seq, **base}
                try:
                    cursor.execute(
                        """
                        INSERT INTO agent_events (session_id, seq, payload)
                        VALUES (%s, %s, %s)
                        """,
                        (
                            session.session_id,
                            next_seq,
                            json.dumps(payload, ensure_ascii=False),
                        ),
                    )
                    # Keep durable meta from lagging behind agent_events. Only
                    # advance; never clobber a higher seq written by a peer.
                    cursor.execute(
                        """
                        UPDATE agent_sessions
                        SET state = jsonb_set(
                                COALESCE(state, '{}'::jsonb),
                                '{event_seq}',
                                to_jsonb(%s::int),
                                true
                            ),
                            updated_at = %s
                        WHERE session_id = %s
                          AND deleted_at IS NULL
                          AND COALESCE((state->>'event_seq')::int, 0) < %s
                        """,
                        (next_seq, _now(), session.session_id, next_seq),
                    )
                    connection.commit()
                except Exception as exc:
                    connection.rollback()
                    is_unique = (
                        unique_violation is not None and isinstance(exc, unique_violation)
                    ) or str(getattr(exc, "sqlstate", "") or "") == "23505"
                    if not is_unique:
                        raise
                    last_error = exc
                    # Align memory with the conflict so the next attempt advances.
                    session.event_seq = max(int(session.event_seq or 0), max_seq, next_seq)
                    continue

            session.event_seq = max(int(session.event_seq or 0), next_seq)
            stamp = self._fetch_updated_at(session.session_id)
            if stamp is not None:
                with self._lock:
                    self._sessions[session.session_id] = session
                    self._session_updated[session.session_id] = stamp
            self._redis.publish_event(session.session_id, payload)
            return payload

        raise RuntimeError(
            f"append_event failed after retries for session {session.session_id}"
        ) from last_error

    def read_events(self, session_id: str, *, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT payload FROM agent_events
                WHERE session_id = %s AND seq > %s
                ORDER BY seq
                """,
                (session_id, after_seq),
            )
            rows = cursor.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            out.append(payload)
        return out

    def save_sdf(self, session: AgentSession) -> None:
        if not session.sdf_bytes:
            return
        blob = self._register_blob(
            session.session_id,
            session.sdf_bytes,
            kind="sdf",
            media_type="chemical/x-mdl-sdfile",
        )
        self._persist_meta(session, sdf_blob_id=blob["blob_id"])

    def clear_sdf(self, session: AgentSession) -> None:
        old_blob = self._read_state_field(session.session_id, "sdf_blob_id")
        session.sdf_bytes = None
        session.sdf_filename = ""
        session.sdf_ui_pending = False
        self._persist_meta(session, sdf_blob_id="")
        if old_blob:
            self._delete_blob_record(str(old_blob))

    def persist(self, session: AgentSession) -> None:
        self._persist_meta(session)

    def _read_state_field(self, session_id: str, field: str) -> Any:
        ns_clause, ns_params = self._namespace_clause()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT state->>%s AS value FROM agent_sessions
                WHERE session_id = %s AND deleted_at IS NULL{ns_clause}
                """,
                [field, session_id, *ns_params],
            )
            row = cursor.fetchone()
        return row["value"] if row else None

    def _persist_meta(
        self,
        session: AgentSession,
        *,
        sdf_blob_id: str | None = None,
        extra_blob_refs: dict[str, str] | None = None,
    ) -> None:
        # No advisory lock here: callers that need cross-process atomicity wrap
        # mutations with mutation_lock(). Nested advisory locks across connections
        # deadlock when persist is invoked inside mutation_lock.
        with self._connect() as connection:
            with connection.cursor() as cursor:
                existing = self._fetch_state_row_on_cursor(cursor, session.session_id)
                if self.lease_managed and existing:
                    self._merge_external_state(session, existing.get("state") or {})
                meta, blob_refs = self._session_state_dict(
                    session,
                    existing_state=existing.get("state") if existing else None,
                    sdf_blob_id=sdf_blob_id,
                    extra_blob_refs=extra_blob_refs,
                )
                session.event_seq = int(meta.get("event_seq") or session.event_seq)
                session.revision = int(meta.get("revision") or session.revision)
                now = _now()
                session.updated_at = now
                revision = int(meta.get("revision") or 0)
                from psycopg.types.json import Json

                state_json = Json(meta)
                if existing:
                    cursor.execute(
                        """
                        UPDATE agent_sessions SET
                            client_id = %s,
                            state = %s,
                            revision = %s,
                            updated_at = %s
                        WHERE session_id = %s
                        """,
                        (
                            session.client_id,
                            state_json,
                            revision,
                            now,
                            session.session_id,
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO agent_sessions (
                            session_id, client_id, namespace, state, revision,
                            created_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            session.session_id,
                            session.client_id,
                            self.namespace,
                            state_json,
                            revision,
                            session.created_at,
                            now,
                        ),
                    )
                for artifact_id, blob_id in blob_refs.get("artifact_blobs", {}).items():
                    self._ensure_blob_row(
                        cursor,
                        blob_id=blob_id,
                        session_id=session.session_id,
                        kind="artifact",
                        media_type=(
                            session.artifacts.get(artifact_id).media_type
                            if artifact_id in session.artifacts
                            else "application/octet-stream"
                        ),
                    )
                if blob_refs.get("sdf_blob_id"):
                    self._ensure_blob_row(
                        cursor,
                        blob_id=blob_refs["sdf_blob_id"],
                        session_id=session.session_id,
                        kind="sdf",
                        media_type="chemical/x-mdl-sdfile",
                    )
                for _attachment_id, metadata in (meta.get("staged_attachments") or {}).items():
                    blob_id = str((metadata or {}).get("blob_id") or "")
                    if blob_id:
                        self._ensure_blob_row(
                            cursor,
                            blob_id=blob_id,
                            session_id=session.session_id,
                            kind="attachment",
                            media_type=str(
                                metadata.get("media_type") or "application/octet-stream"
                            ),
                        )
            connection.commit()
        stamp = self._fetch_updated_at(session.session_id) or session.updated_at
        with self._lock:
            self._sessions[session.session_id] = session
            self._session_updated[session.session_id] = stamp

    def _fetch_state_row_on_cursor(self, cursor, session_id: str) -> dict | None:
        ns_clause, ns_params = self._namespace_clause()
        cursor.execute(
            f"""
            SELECT session_id, state, revision, updated_at
            FROM agent_sessions
            WHERE session_id = %s AND deleted_at IS NULL{ns_clause}
            """,
            [session_id, *ns_params],
        )
        row = cursor.fetchone()
        if not row:
            return None
        state = row["state"]
        if isinstance(state, str):
            state = json.loads(state)
        return {"state": state, "revision": int(row["revision"]), "updated_at": row["updated_at"]}

    def _fetch_state_row(self, session_id: str) -> dict | None:
        ns_clause, ns_params = self._namespace_clause()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT session_id, state, revision, updated_at
                FROM agent_sessions
                WHERE session_id = %s AND deleted_at IS NULL{ns_clause}
                """,
                [session_id, *ns_params],
            )
            row = cursor.fetchone()
        if not row:
            return None
        state = row["state"]
        if isinstance(state, str):
            state = json.loads(state)
        return {"state": state, "revision": int(row["revision"]), "updated_at": row["updated_at"]}

    @staticmethod
    def _merge_external_state(session: AgentSession, external: dict[str, Any]) -> None:
        """Merge cross-process signals into a worker's in-memory snapshot.

        Pending-turn membership is owned by the writer that calls persist();
        re-adding turns from a stale DB snapshot would undo cancel/edit.
        """
        external_active = external.get("active_run")
        if isinstance(external_active, dict) and isinstance(session.active_run, dict):
            if external_active.get("run_id") == session.active_run.get("run_id"):
                external_status = str(external_active.get("status") or "")
                local_status = str(session.active_run.get("status") or "")
                if external_status == "cancel_requested" and local_status in {"queued", "running"}:
                    session.active_run.update(external_active)
        if isinstance(external.get("resume_context"), dict):
            latest = str((external.get("resume_context") or {}).get("latest_guidance") or "")
            if latest:
                session.resume_context = dict(external["resume_context"])
        checkpoints = {
            str(item.get("checkpoint_id") or ""): item
            for item in session.tool_checkpoints
            if item.get("checkpoint_id")
        }
        for item in external.get("tool_checkpoints") or []:
            if not isinstance(item, dict):
                continue
            checkpoint_id = str(item.get("checkpoint_id") or "")
            current = checkpoints.get(checkpoint_id)
            if current is None:
                session.tool_checkpoints.append(dict(item))
            elif current.get("status") == "running" and item.get("status") == "interrupted":
                current.update(item)
        session.revision = max(int(session.revision), int(external.get("revision") or 0))

    def _session_state_dict(
        self,
        session: AgentSession,
        *,
        existing_state: dict[str, Any] | None,
        sdf_blob_id: str | None = None,
        extra_blob_refs: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        existing = existing_state or {}
        artifact_blobs = dict(existing.get("artifact_blobs") or {})
        artifact_meta = dict(existing.get("artifact_meta") or {})
        if extra_blob_refs:
            artifact_blobs.update(extra_blob_refs)
        for artifact_id, artifact in session.artifacts.items():
            artifact_meta[artifact_id] = {
                k: v for k, v in asdict(artifact).items() if k != "content"
            }
            if artifact_id not in artifact_blobs and existing.get("artifact_blobs", {}).get(artifact_id):
                artifact_blobs[artifact_id] = existing["artifact_blobs"][artifact_id]
        resolved_sdf_blob = (
            sdf_blob_id
            if sdf_blob_id is not None
            else existing.get("sdf_blob_id") or ""
        )
        if sdf_blob_id == "":
            resolved_sdf_blob = ""
        has_sdf = bool(session.sdf_bytes) or bool(resolved_sdf_blob)
        meta = {
            "session_id": session.session_id,
            "client_id": session.client_id,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "profile_id": session.profile_id,
            "title": session.title,
            "installed_catalog": list(session.installed_catalog),
            "installed_scp_skills": session.installed_scp_skills,
            "sdf_filename": session.sdf_filename,
            "has_sdf": has_sdf,
            "sdf_ui_pending": bool(session.sdf_ui_pending) and bool(session.sdf_filename),
            "top_n": session.top_n,
            "pending_top_confirm": session.pending_top_confirm,
            "pending_action": session.pending_action,
            "pending_goal": session.pending_goal,
            "pending_install": session.pending_install,
            "last_run_id": session.last_run_id,
            "last_selection_sha256": session.last_selection_sha256,
            "last_config_hash": session.last_config_hash,
            "last_input_sha256": session.last_input_sha256,
            "frozen_ranking": session.frozen_ranking,
            "run_history": session.run_history[-20:],
            "active_plan": session.active_plan,
            "plan_history": session.plan_history[-20:],
            "working_memory": session.working_memory[-24:],
            "approval_grants": session.approval_grants[-24:],
            "agent_run_state": session.agent_run_state,
            "last_molecule_index": session.last_molecule_index,
            "last_mechanism_job_id": session.last_mechanism_job_id,
            "artifact_ids": list(session.artifacts.keys()),
            "artifact_meta": artifact_meta,
            "artifact_blobs": artifact_blobs,
            "sdf_blob_id": resolved_sdf_blob,
            "messages": session.messages[-50:],
            # Never persist a regressing counter if another writer advanced meta.
            "event_seq": max(
                int(session.event_seq or 0),
                int(existing.get("event_seq") or 0),
            ),
            "active_run": session.active_run,
            "pending_turns": session.pending_turns[-8:],
            "staged_attachments": session.staged_attachments,
            "resume_context": session.resume_context,
            "context_summary": session.context_summary,
            "agent_run_history": session.agent_run_history[-30:],
            "tool_checkpoints": session.tool_checkpoints[-100:],
            "revision": session.revision,
        }
        blob_refs = {
            "artifact_blobs": artifact_blobs,
            "sdf_blob_id": resolved_sdf_blob,
        }
        return meta, blob_refs

    def _register_blob(
        self,
        session_id: str,
        content: bytes,
        *,
        kind: str,
        media_type: str,
    ) -> dict:
        blob = self._blobs.put(content, kind=kind, media_type=media_type, session_id=session_id)
        with self._connect() as connection, connection.cursor() as cursor:
            self._ensure_blob_row(
                cursor,
                blob_id=blob["blob_id"],
                session_id=session_id,
                kind=kind,
                media_type=media_type,
                blob_meta=blob,
            )
            connection.commit()
        return blob

    def _ensure_blob_row(
        self,
        cursor,
        *,
        blob_id: str,
        session_id: str,
        kind: str,
        media_type: str,
        blob_meta: dict | None = None,
    ) -> None:
        meta = blob_meta or {}
        relative_path = str(meta.get("relative_path") or f"{kind}/{blob_id[:2]}/{blob_id}")
        sha256 = str(meta.get("sha256") or "")
        byte_size = int(meta.get("size") or 0)
        if not sha256 and blob_id:
            try:
                content = self._blobs.get(blob_id)
                sha256 = hashlib.sha256(content).hexdigest()
                byte_size = len(content)
            except FileNotFoundError:
                pass
        cursor.execute(
            """
            INSERT INTO agent_blobs (
                blob_id, session_id, kind, media_type, byte_size,
                content_sha256, relative_path
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (blob_id) DO NOTHING
            """,
            (blob_id, session_id, kind, media_type, byte_size, sha256, relative_path),
        )

    def _delete_blob_record(self, blob_id: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM agent_blobs WHERE blob_id = %s", (blob_id,))
            connection.commit()
        self._blobs.delete(blob_id)

    def _load_session(self, session_id: str) -> AgentSession | None:
        row = self._fetch_state_row(session_id)
        if not row:
            return None
        meta = row["state"]
        raw_molecule_index = meta.get("last_molecule_index") or {}
        molecule_index: dict[str, list[dict[str, Any]]] = {}
        if isinstance(raw_molecule_index, dict):
            for raw_id, raw_entries in raw_molecule_index.items():
                molecule_id = str(raw_id or "").strip()
                if not molecule_id:
                    continue
                entries = raw_entries if isinstance(raw_entries, list) else [raw_entries]
                clean_entries: list[dict[str, Any]] = []
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    raw_steps = entry.get("standardization_steps") or []
                    steps = raw_steps if isinstance(raw_steps, (list, tuple)) else []
                    clean_entries.append(
                        {
                            "molecule_id": str(entry.get("molecule_id") or molecule_id),
                            "inchikey": (
                                str(entry["inchikey"]) if entry.get("inchikey") else None
                            ),
                            "cas": str(entry["cas"]) if entry.get("cas") else None,
                            "smiles": (
                                str(entry["smiles"]) if entry.get("smiles") else None
                            ),
                            "original_smiles": (
                                str(entry["original_smiles"])
                                if entry.get("original_smiles")
                                else None
                            ),
                            "standardization_steps": [
                                str(step) for step in steps if str(step)
                            ],
                        }
                    )
                if clean_entries:
                    molecule_index[molecule_id] = clean_entries
        session = AgentSession(
            session_id=session_id,
            client_id=str(meta.get("client_id") or ""),
            created_at=str(meta.get("created_at") or _now()),
            updated_at=str(meta.get("updated_at") or meta.get("created_at") or _now()),
            profile_id=str(meta.get("profile_id") or "competition_masld"),
            title=str(meta.get("title") or ""),
            installed_catalog=list(meta.get("installed_catalog") or []),
            installed_scp_skills={
                str(k): dict(v)
                for k, v in (meta.get("installed_scp_skills") or {}).items()
                if isinstance(v, dict)
            },
            sdf_filename=str(meta.get("sdf_filename") or ""),
            sdf_ui_pending=bool(meta.get("sdf_ui_pending")),
            top_n=int(meta.get("top_n") or 10),
            pending_top_confirm=(
                dict(meta["pending_top_confirm"])
                if isinstance(meta.get("pending_top_confirm"), dict)
                else None
            ),
            pending_action=(
                dict(meta["pending_action"])
                if isinstance(meta.get("pending_action"), dict)
                else None
            ),
            pending_goal=(
                dict(meta["pending_goal"]) if isinstance(meta.get("pending_goal"), dict) else None
            ),
            pending_install=(
                dict(meta["pending_install"])
                if isinstance(meta.get("pending_install"), dict)
                else None
            ),
            last_run_id=str(meta.get("last_run_id") or ""),
            last_selection_sha256=str(meta.get("last_selection_sha256") or ""),
            last_config_hash=str(meta.get("last_config_hash") or ""),
            last_input_sha256=str(meta.get("last_input_sha256") or ""),
            frozen_ranking=(
                dict(meta["frozen_ranking"])
                if isinstance(meta.get("frozen_ranking"), dict)
                else None
            ),
            run_history=[
                dict(item) for item in meta.get("run_history") or [] if isinstance(item, dict)
            ][-20:],
            active_plan=(
                dict(meta["active_plan"]) if isinstance(meta.get("active_plan"), dict) else None
            ),
            plan_history=[
                dict(item) for item in meta.get("plan_history") or [] if isinstance(item, dict)
            ][-20:],
            working_memory=[
                dict(item) for item in meta.get("working_memory") or [] if isinstance(item, dict)
            ][-24:],
            approval_grants=[
                dict(item) for item in meta.get("approval_grants") or [] if isinstance(item, dict)
            ][-24:],
            agent_run_state=(
                dict(meta["agent_run_state"])
                if isinstance(meta.get("agent_run_state"), dict)
                else None
            ),
            last_molecule_index=molecule_index,
            last_mechanism_job_id=str(meta.get("last_mechanism_job_id") or ""),
            messages=list(meta.get("messages") or []),
            event_seq=max(
                int(meta.get("event_seq") or 0),
                self._max_event_seq(session_id),
            ),
            active_run=(
                dict(meta["active_run"]) if isinstance(meta.get("active_run"), dict) else None
            ),
            pending_turns=[
                dict(item) for item in meta.get("pending_turns") or [] if isinstance(item, dict)
            ][-8:],
            staged_attachments={
                str(key): dict(value)
                for key, value in (meta.get("staged_attachments") or {}).items()
                if isinstance(value, dict)
            },
            resume_context=(
                dict(meta["resume_context"])
                if isinstance(meta.get("resume_context"), dict)
                else None
            ),
            context_summary=(
                dict(meta["context_summary"])
                if isinstance(meta.get("context_summary"), dict)
                else None
            ),
            agent_run_history=[
                dict(item) for item in meta.get("agent_run_history") or [] if isinstance(item, dict)
            ][-30:],
            tool_checkpoints=[
                dict(item) for item in meta.get("tool_checkpoints") or [] if isinstance(item, dict)
            ][-100:],
            revision=int(meta.get("revision") or 0),
        )
        sdf_blob_id = str(meta.get("sdf_blob_id") or "")
        if sdf_blob_id:
            try:
                session.sdf_bytes = self._blobs.get(sdf_blob_id)
            except FileNotFoundError:
                session.sdf_bytes = None
                session.sdf_ui_pending = False
                if not session.sdf_bytes:
                    session.sdf_filename = ""
        else:
            session.sdf_ui_pending = False
            if not session.sdf_bytes:
                session.sdf_filename = ""
        artifact_blobs = meta.get("artifact_blobs") or {}
        artifact_meta = meta.get("artifact_meta") or {}
        for artifact_id in meta.get("artifact_ids") or []:
            am = artifact_meta.get(artifact_id) or {}
            blob_id = artifact_blobs.get(artifact_id)
            if not blob_id:
                continue
            try:
                content = self._blobs.get(str(blob_id))
            except FileNotFoundError:
                continue
            art = Artifact(
                artifact_id=str(am.get("artifact_id") or artifact_id),
                kind=am.get("kind") or "bin",
                filename=am.get("filename") or "artifact.bin",
                title=am.get("title") or "",
                subtitle=am.get("subtitle") or "",
                media_type=am.get("media_type") or "application/octet-stream",
                content=content,
                created_at=am.get("created_at") or _now(),
            )
            session.artifacts[art.artifact_id] = art
        return session

    def stage_attachment(
        self,
        session: AgentSession,
        *,
        filename: str,
        content: bytes,
        media_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        self.cleanup_staged_attachments(session)
        live = [
            item
            for item in session.staged_attachments.values()
            if isinstance(item, dict) and item.get("state") in {"draft", "queued", "active"}
        ]
        if len(live) >= 8:
            raise ValueError("每个会话最多保留 8 个待处理附件")
        if sum(int(item.get("size") or 0) for item in live) + len(content) > 2 * 1024 * 1024 * 1024:
            raise ValueError("每个会话的待处理附件总量不能超过 2 GB")
        attachment_id = f"att-{uuid.uuid4().hex[:12]}"
        digest = hashlib.sha256(content).hexdigest()
        from agent.memory.attachments import attachment_kind_for_filename

        kind = attachment_kind_for_filename(filename)
        blob = self._register_blob(
            session.session_id,
            content,
            kind="attachment",
            media_type=media_type or "application/octet-stream",
        )
        metadata = {
            "attachment_id": attachment_id,
            "filename": filename or "attachment.bin",
            "media_type": media_type or "application/octet-stream",
            "size": len(content),
            "sha256": digest,
            "created_at": _now(),
            "state": "draft",
            "blob_id": blob["blob_id"],
            "kind": kind,
        }
        session.staged_attachments[attachment_id] = metadata
        self._persist_meta(session)
        return dict(metadata)

    def cleanup_staged_attachments(self, session: AgentSession, *, ttl_seconds: int = 86_400) -> int:
        now = datetime.now(timezone.utc)
        removed = 0
        for attachment_id, metadata in list(session.staged_attachments.items()):
            if not isinstance(metadata, dict) or metadata.get("state") not in {"draft", "consumed"}:
                continue
            raw = str(metadata.get("consumed_at") or metadata.get("created_at") or "")
            try:
                created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                created = now
            if (now - created).total_seconds() < ttl_seconds:
                continue
            blob_id = str(metadata.get("blob_id") or "")
            session.staged_attachments.pop(attachment_id, None)
            if blob_id:
                self._delete_blob_record(blob_id)
            removed += 1
        if removed:
            self._persist_meta(session)
        return removed

    def read_staged_attachment(
        self,
        session: AgentSession,
        attachment_id: str,
    ) -> tuple[dict[str, Any], bytes] | None:
        metadata = session.staged_attachments.get(attachment_id)
        if not isinstance(metadata, dict):
            return None
        blob_id = str(metadata.get("blob_id") or "")
        if not blob_id:
            return None
        try:
            content = self._blobs.get(blob_id)
        except FileNotFoundError:
            return None
        return dict(metadata), content

    def delete_staged_attachment(self, session: AgentSession, attachment_id: str) -> bool:
        metadata = session.staged_attachments.get(attachment_id)
        if not isinstance(metadata, dict) or metadata.get("state") != "draft":
            return False
        blob_id = str(metadata.get("blob_id") or "")
        session.staged_attachments.pop(attachment_id, None)
        if blob_id:
            self._delete_blob_record(blob_id)
        self._persist_meta(session)
        return True
