"""Agent session and artifact dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
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
    #: SCP skills are independently enabled/disabled and keep their locks for audit.
    installed_scp_skills: dict[str, dict[str, Any]] = field(default_factory=dict)
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
    #: Compact JSON snapshot of the latest freeze (scores + run identity).
    #: Hydrates ``last_result`` across workers without pickling PipelineResult.
    frozen_ranking: dict[str, Any] | None = None
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
    #: Durable FIFO turns waiting behind ``active_run``. Guidance turns may be
    #: inserted at the front, but normal queued turns are capped by the API.
    pending_turns: list[dict[str, Any]] = field(default_factory=list)
    #: Metadata for files uploaded while a Run is active. Bytes live under the
    #: session directory and are only bound to the session when their Turn starts.
    staged_attachments: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Most recent compact resume envelope built when guidance interrupts a Run.
    resume_context: dict[str, Any] | None = None
    #: Deterministic compact history used when the raw conversation exceeds the
    #: prompt budget. Exact recent turns and operational identifiers stay separate.
    context_summary: dict[str, Any] | None = None
    #: Terminal Agent Run snapshots retained for exact retry lineage.
    agent_run_history: list[dict[str, Any]] = field(default_factory=list)
    #: Tool-level checkpoints. Successful entries may be reused by retry Runs;
    #: interrupted/failed entries are recorded as pending re-execution.
    tool_checkpoints: list[dict[str, Any]] = field(default_factory=list)
    #: Incremented by session-scoped input/config mutations. A Run freezes the
    #: revision it started from so concurrent writes can never be mistaken for
    #: part of that Run.
    revision: int = 0
