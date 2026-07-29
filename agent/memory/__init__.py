"""FileRunStore：会话 / 事件 / 产物落盘（赛期默认，遵守技术栈冻结）。"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_runs_root() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "agent_runs"


@dataclass
class Artifact:
    artifact_id: str
    kind: str
    filename: str
    title: str
    subtitle: str
    media_type: str
    content: bytes
    created_at: str = field(default_factory=_now)


@dataclass
class AgentSession:
    session_id: str
    #: Stable browser installation id. It partitions server-side history so a
    #: shared NAS deployment does not expose one browser's sessions to another.
    client_id: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    profile_id: str = "competition_masld"
    title: str = ""
    installed_catalog: list[str] = field(default_factory=list)
    sdf_bytes: bytes | None = None
    sdf_filename: str = ""
    #: True after upload until the next user message "consumes" it into the chat UI.
    #: SDF bytes remain for same-session tool use even after this flips False.
    sdf_ui_pending: bool = False
    top_n: int = 10
    #: Awaiting user confirm to run at capped top_n after over-limit request.
    pending_top_confirm: dict[str, Any] | None = None
    #: Multi-turn executable request whose required slots are not complete yet.
    #: This is operational dialogue state (deliverables and missing inputs),
    #: never a ranking result or a substitute for tool observations.
    pending_action: dict[str, Any] | None = None
    #: A requested screening configuration that is not executable under the
    #: current tool contract. It must be clarified or explicitly discarded;
    #: later exports must not silently run the default pipeline instead.
    pending_goal: dict[str, Any] | None = None
    last_run_id: str = ""
    last_selection_sha256: str = ""
    last_config_hash: str = ""
    last_input_sha256: str = ""
    last_result: Any = None
    #: Durable summaries of frozen runs. ``last_result`` remains the hot,
    #: in-memory object; this history lets future planning distinguish runs
    #: after a process restart without serializing mutable score objects.
    run_history: list[dict[str, Any]] = field(default_factory=list)
    #: Current plan plus recent completed plans, persisted as plain JSON so a
    #: restarted runtime can explain what was attempted and observed.
    active_plan: dict[str, Any] | None = None
    plan_history: list[dict[str, Any]] = field(default_factory=list)
    #: Session-scoped working memory for recent Agent Loop iterations.  It
    #: stores compact task/call/observation/decision records, never scientific
    #: ranking objects, and is deleted with the session.
    working_memory: list[dict[str, Any]] = field(default_factory=list)
    #: Exact, short-lived HITL grants. Every grant binds one tool, argument
    #: hash and session; it is consumed once and never authorizes a different
    #: parameter set.
    approval_grants: list[dict[str, Any]] = field(default_factory=list)
    #: Last persisted Agent-turn controller snapshot. This is operational
    #: state only and must never contain ranking/scoring objects.
    agent_run_state: dict[str, Any] | None = None
    #: 当前筛选 Run 的最小身份索引；可跨进程恢复，绝不包含评分或排名字段。
    last_molecule_index: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    last_mechanism_job_id: str = ""
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    event_seq: int = 0
    #: Durable state for the latest turn. Active statuses are queued/running/
    #: cancel_requested; terminal snapshots are retained for refresh recovery.
    active_run: dict[str, Any] | None = None
    #: Incremented by session-scoped input/config mutations. A Run freezes the
    #: revision it started from so concurrent writes can never be mistaken for
    #: part of that Run.
    revision: int = 0


class FileRunStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_runs_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._sessions: dict[str, AgentSession] = {}

    def _clients_dir(self) -> Path:
        return self.root / "_clients"

    def register_client(self, client_id: str) -> None:
        """Persist a browser identity without creating an empty conversation."""
        if not client_id:
            return
        clients_dir = self._clients_dir()
        clients_dir.mkdir(parents=True, exist_ok=True)
        path = clients_dir / f"{client_id}.json"
        if path.is_file():
            return
        path.write_text(
            json.dumps({"client_id": client_id, "created_at": _now()}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _session_dir(self, session_id: str) -> Path:
        return self.root / session_id

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
        self._persist_meta(session)
        return session

    def get(self, session_id: str) -> AgentSession | None:
        with self._lock:
            if session_id in self._sessions:
                return self._sessions[session_id]
        loaded = self._load_session(session_id)
        if loaded:
            with self._lock:
                self._sessions[session_id] = loaded
        return loaded

    def list_sessions(
        self,
        *,
        limit: int = 50,
        client_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Scan FileRunStore root for session metas (newest first)."""
        items: list[dict[str, Any]] = []
        if not self.root.is_dir():
            return items
        for path in self.root.iterdir():
            if not path.is_dir() or path == self._clients_dir():
                continue
            meta_path = path / "meta.json"
            if not meta_path.is_file():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if client_id is not None and meta.get("client_id") != client_id:
                continue
            preview = ""
            messages = meta.get("messages") or []
            for msg in reversed(messages):
                if msg.get("role") == "user" and msg.get("text"):
                    preview = str(msg["text"]).strip()
                    break
            title = str(meta.get("title") or "").strip() or preview[:48] or "未命名对话"
            items.append(
                {
                    "session_id": meta.get("session_id") or path.name,
                    "title": title,
                    "preview": preview[:120],
                    "created_at": meta.get("created_at") or "",
                    "updated_at": meta.get("updated_at") or meta.get("created_at") or "",
                    "sdf_filename": meta.get("sdf_filename") or "",
                    "has_sdf": bool(meta.get("has_sdf")),
                    "profile_id": meta.get("profile_id") or "competition_masld",
                    "artifact_count": len(meta.get("artifact_ids") or []),
                    "event_seq": int(meta.get("event_seq") or 0),
                    "run_status": str(
                        (meta.get("active_run") or {}).get("status") or ""
                    ),
                    "active_run_id": (
                        str((meta.get("active_run") or {}).get("run_id") or "")
                        if str((meta.get("active_run") or {}).get("status") or "")
                        in {"queued", "running", "cancel_requested"}
                        else ""
                    ),
                }
            )
        items.sort(key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)
        return items[: max(1, limit)]

    def client_exists(self, client_id: str) -> bool:
        """Return whether a browser id is registered or owns persisted sessions."""
        if not client_id or not self.root.is_dir():
            return False
        if (self._clients_dir() / f"{client_id}.json").is_file():
            return True
        for path in self.root.iterdir():
            meta_path = path / "meta.json"
            if not path.is_dir() or not meta_path.is_file():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if meta.get("client_id") == client_id:
                return True
        return False

    def read_events(self, session_id: str, *, after_seq: int = 0) -> list[dict[str, Any]]:
        path = self._session_dir(session_id) / "events.jsonl"
        if not path.is_file():
            return []
        out: list[dict[str, Any]] = []
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                seq = int(ev.get("seq") or 0)
                if seq > after_seq:
                    out.append(ev)
        return out

    def set_title(self, session: AgentSession, title: str) -> None:
        session.title = (title or "").strip()[:80]
        self._persist_meta(session)

    def delete_session(self, session_id: str) -> bool:
        """Remove session from memory and delete its on-disk directory."""
        import shutil

        with self._lock:
            self._sessions.pop(session_id, None)
        d = self._session_dir(session_id)
        if not d.exists():
            return False
        shutil.rmtree(d, ignore_errors=True)
        return True

    def clear_sessions(self, *, client_id: str | None = None) -> int:
        """Remove matching persisted sessions and return the number removed."""
        import shutil

        if not self.root.is_dir():
            return 0
        session_dirs: list[Path] = []
        for path in self.root.iterdir():
            if not path.is_dir() or path == self._clients_dir():
                continue
            if client_id is None:
                session_dirs.append(path)
                continue
            meta_path = path / "meta.json"
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if meta.get("client_id") == client_id:
                session_dirs.append(path)
        with self._lock:
            if client_id is None:
                self._sessions.clear()
            else:
                for session_id, session in list(self._sessions.items()):
                    if session.client_id == client_id:
                        self._sessions.pop(session_id, None)
        for path in session_dirs:
            shutil.rmtree(path, ignore_errors=True)
        return len(session_dirs)

    def put_artifact(self, session: AgentSession, artifact: Artifact) -> Artifact:
        with self._lock:
            session.artifacts[artifact.artifact_id] = artifact
        self._write_artifact(session, artifact)
        self._persist_meta(session)
        return artifact

    def append_event(self, session: AgentSession, event: dict[str, Any]) -> dict[str, Any]:
        session.event_seq += 1
        payload = {"seq": session.event_seq, **event}
        # Event time is persisted separately from session.updated_at so the
        # frontend can reconstruct a completed turn's elapsed time on reload.
        payload.setdefault("occurred_at", _now())
        path = self._session_dir(session.session_id) / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return payload

    def save_sdf(self, session: AgentSession) -> None:
        if not session.sdf_bytes:
            return
        d = self._session_dir(session.session_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "library.sdf").write_bytes(session.sdf_bytes)
        self._persist_meta(session)

    def clear_sdf(self, session: AgentSession) -> None:
        session.sdf_bytes = None
        session.sdf_filename = ""
        session.sdf_ui_pending = False
        d = self._session_dir(session.session_id)
        sdf_path = d / "library.sdf"
        if sdf_path.is_file():
            sdf_path.unlink()
        self._persist_meta(session)

    def persist(self, session: AgentSession) -> None:
        self._persist_meta(session)

    def _persist_meta(self, session: AgentSession) -> None:
        d = self._session_dir(session.session_id)
        d.mkdir(parents=True, exist_ok=True)
        session.updated_at = _now()
        meta = {
            "session_id": session.session_id,
            "client_id": session.client_id,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "profile_id": session.profile_id,
            "title": session.title,
            "installed_catalog": list(session.installed_catalog),
            "sdf_filename": session.sdf_filename,
            "has_sdf": bool(session.sdf_bytes) or (d / "library.sdf").is_file(),
            "sdf_ui_pending": bool(session.sdf_ui_pending) and bool(session.sdf_filename),
            "top_n": session.top_n,
            "pending_top_confirm": session.pending_top_confirm,
            "pending_action": session.pending_action,
            "pending_goal": session.pending_goal,
            "last_run_id": session.last_run_id,
            "last_selection_sha256": session.last_selection_sha256,
            "last_config_hash": session.last_config_hash,
            "last_input_sha256": session.last_input_sha256,
            "run_history": session.run_history[-20:],
            "active_plan": session.active_plan,
            "plan_history": session.plan_history[-20:],
            "working_memory": session.working_memory[-24:],
            "approval_grants": session.approval_grants[-24:],
            "agent_run_state": session.agent_run_state,
            "last_molecule_index": session.last_molecule_index,
            "last_mechanism_job_id": session.last_mechanism_job_id,
            "artifact_ids": list(session.artifacts.keys()),
            "messages": session.messages[-50:],
            "event_seq": session.event_seq,
            "active_run": session.active_run,
            "revision": session.revision,
        }
        meta_path = d / "meta.json"
        tmp_path = d / f".meta.{uuid.uuid4().hex}.tmp"
        tmp_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(meta_path)

    def _write_artifact(self, session: AgentSession, artifact: Artifact) -> None:
        d = self._session_dir(session.session_id) / "artifacts"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{artifact.artifact_id}.bin").write_bytes(artifact.content)
        meta = {k: v for k, v in asdict(artifact).items() if k != "content"}
        (d / f"{artifact.artifact_id}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_session(self, session_id: str) -> AgentSession | None:
        d = self._session_dir(session_id)
        meta_path = d / "meta.json"
        if not meta_path.is_file():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
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
                                str(step)
                                for step in steps
                                if str(step)
                            ],
                        }
                    )
                if clean_entries:
                    molecule_index[molecule_id] = clean_entries
        session = AgentSession(
            session_id=session_id,
            client_id=str(meta.get("client_id") or ""),
            created_at=str(meta.get("created_at") or _now()),
            updated_at=str(
                meta.get("updated_at") or meta.get("created_at") or _now()
            ),
            profile_id=str(meta.get("profile_id") or "competition_masld"),
            title=str(meta.get("title") or ""),
            installed_catalog=list(meta.get("installed_catalog") or []),
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
            pending_goal=(dict(meta["pending_goal"]) if isinstance(meta.get("pending_goal"), dict) else None),
            last_run_id=str(meta.get("last_run_id") or ""),
            last_selection_sha256=str(meta.get("last_selection_sha256") or ""),
            last_config_hash=str(meta.get("last_config_hash") or ""),
            last_input_sha256=str(meta.get("last_input_sha256") or ""),
            run_history=[
                dict(item)
                for item in meta.get("run_history") or []
                if isinstance(item, dict)
            ][-20:],
            active_plan=(dict(meta["active_plan"]) if isinstance(meta.get("active_plan"), dict) else None),
            plan_history=[
                dict(item)
                for item in meta.get("plan_history") or []
                if isinstance(item, dict)
            ][-20:],
            working_memory=[
                dict(item)
                for item in meta.get("working_memory") or []
                if isinstance(item, dict)
            ][-24:],
            approval_grants=[
                dict(item)
                for item in meta.get("approval_grants") or []
                if isinstance(item, dict)
            ][-24:],
            agent_run_state=(
                dict(meta["agent_run_state"])
                if isinstance(meta.get("agent_run_state"), dict)
                else None
            ),
            last_molecule_index=molecule_index,
            last_mechanism_job_id=str(meta.get("last_mechanism_job_id") or ""),
            messages=list(meta.get("messages") or []),
            event_seq=int(meta.get("event_seq") or 0),
            active_run=(
                dict(meta["active_run"])
                if isinstance(meta.get("active_run"), dict)
                else None
            ),
            revision=int(meta.get("revision") or 0),
        )
        # An active Run loaded from disk cannot still have an owning worker in
        # this process. Close it explicitly instead of leaving the UI forever
        # in a false "running" state after an API restart.
        interrupted_on_load = bool(
            isinstance(session.active_run, dict)
            and str(session.active_run.get("status") or "")
            in {"queued", "running", "cancel_requested"}
        )
        if interrupted_on_load:
            run_id = str(session.active_run.get("run_id") or "")
            session.active_run["status"] = "interrupted"
            session.active_run["ended_at"] = _now()
            session.active_run["heartbeat_at"] = _now()
            interrupted = self.append_event(
                session,
                {
                    "type": "run_interrupted",
                    "run_id": run_id,
                    "turn_id": str(session.active_run.get("turn_id") or run_id),
                    "detail": "服务进程重启，本轮执行已中断，可重新发送本轮请求。",
                },
            )
            session.active_run["last_event_seq"] = int(interrupted.get("seq") or 0)
        sdf_path = d / "library.sdf"
        if sdf_path.is_file():
            session.sdf_bytes = sdf_path.read_bytes()
        else:
            # Never inherit another session's library; clear stale pending flags.
            session.sdf_ui_pending = False
            if not session.sdf_bytes:
                session.sdf_filename = ""
        art_dir = d / "artifacts"
        if art_dir.is_dir():
            for meta_file in art_dir.glob("*.json"):
                am = json.loads(meta_file.read_text(encoding="utf-8"))
                bin_path = art_dir / f"{am['artifact_id']}.bin"
                if not bin_path.is_file():
                    continue
                art = Artifact(
                    artifact_id=am["artifact_id"],
                    kind=am.get("kind") or "bin",
                    filename=am.get("filename") or "artifact.bin",
                    title=am.get("title") or "",
                    subtitle=am.get("subtitle") or "",
                    media_type=am.get("media_type") or "application/octet-stream",
                    content=bin_path.read_bytes(),
                    created_at=am.get("created_at") or _now(),
                )
                session.artifacts[art.artifact_id] = art
        if interrupted_on_load:
            self._persist_meta(session)
        return session


STORE = FileRunStore()
