"""Agent Loop：意图 → Skill 计划 → Tool 调用 → 流式事件（可落盘）。"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import queue
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

from agent.intent import (
    AgentIntent,
    MentionRef,
    extract_ranking_positions,
    parse_intent,
    ranking_position_subject_fallback,
    ranking_question_fallback,
)
from agent.memory import STORE, AgentSession, Artifact, PostgresRunStore
from agent.memory.frozen_ranking import (
    ensure_session_last_result,
    has_durable_freeze,
    snapshot_from_result,
)
from agent.policy import claim_ceiling_default
from agent.registry import get_registry
from agent.runtime.decide import llm_json_decision, llm_json_object
from agent.memory.attachments import (
    format_attachment_context,
    summarize_attachment_for_context,
)
from agent.runtime.cancellable_call import CallCancelled, cancel_scope, run_cancellable, wait_interruptible
from agent.runtime.context import build_context_window
from agent.runtime.governance import (
    GovernanceDecision,
    ToolGovernance,
    frozen_ranking_mutation_requested,
    grant_approval,
)
from agent.runtime.observation import normalize_tool_end
from agent.runtime.planning import (
    PlanStep,
    llm_plan_request,
    plan_for_skills,
    session_capabilities,
)
from agent.runtime.reply import format_ranking_explanation, format_run_completion
from agent.runtime.scheduler import RunBudget, RunController, canonical_args_hash, utc_now
from agent.runtime.task_graph import TaskGraph
from agent.runtime.task_router import TaskRouter
from agent.runtime.observation_validator import ObservationValidator
from agent.runtime.verification import evidence_correction, verify_assistant_claims
from plugins.catalog_dispatch import (
    TOOL_HANDLERS,
    dispatch_tool,
    iter_installed_enrichment,
)
from plugins.scp_hub.catalog import SCPCatalog
from plugins.scp_hub.registry import SCPRegistryManager
from plugins.scp_hub.jobs import SCPJobManager
from plugins.molmind_core.tools.scientific import run_score_and_rank
from plugins.molmind_core.scientific.mechanism.jobs import cancel_job, get_job, start_mechanism_job
from plugins.molmind_core.scientific.pipeline.export import reserve_shortage_note
from plugins.molmind_core.scientific.pipeline.run_log import RunLogEntry
from plugins.molmind_core.scientific.evidence_gateway.contract import content_sha256


def _aid() -> str:
    return uuid.uuid4().hex[:12]


_EVIDENCE_MENTION_IDS = frozenset({"query_evidence", "masld_explain"})
_DEFAULT_CONFIG_EXECUTION_RE = re.compile(
    r"(?:使用|按|用|按照).{0,12}(?:当前)?默认.{0,20}(?:筛选|配置).{0,32}"
    r"(?:生成|导出|筛选|运行|top)",
    re.I,
)
_DIRECT_DELIVERABLE_RE = re.compile(
    # Bare「筛选」alone is too weak: it also appears in discuss/defer turns.
    # Positive deliverable cues stay structural; execution-vs-defer is gated by LLM.
    r"(?:生成|导出|运行|重跑|重新跑|重新筛选|开始跑|制作|做一份|出一份)|"
    r"(?:希望|想要|需要|请|给我|帮我).{0,40}"
    r"(?:csv|候选|提名|清单|短名单|候补|结果包|交卷包|候选包|bundle|top)|"
    r"(?:默认).{0,24}(?:配置|筛选).{0,24}top",
    re.I,
)
_PENDING_TOP_N_REPLY_RE = re.compile(r"^\s*(?:top\s*)?(\d{1,3})\s*(?:个|名)?\s*$", re.I)
_PENDING_CANCEL_RE = re.compile(r"^\s*(?:取消|算了|不用了?|不要了?|停止)(?:[。！!])?\s*$", re.I)
# Affirm/status synonym tables are LLM-down only (see _classify_pending_continuation).
_PENDING_AFFIRM_RE = re.compile(
    r"^\s*(?:"
    r"需要|要|是|对|可以|好|好的|行|继续|开始|现在呢|现在可以了?"
    r"|提供了|已经提供了?|已提供|已上传|上传了"
    r"|(?:我)?(?:已经)?(?:提供|上传)了?.{0,40}"
    r")(?:[。！!？?])?\s*$",
    re.I,
)
_PENDING_STATUS_RE = re.compile(
    r"(?:好了(?:吗|嘛)?|完成了?(?:吗|嘛)?|进度|开始了?(?:吗|嘛)?|还没好|怎么样了)",
    re.I,
)
_COMPOUND_MAX_ITERATIONS = 3
_BRANCH_ASSISTANT_CAPTURE: ContextVar[list[str] | None] = ContextVar(
    "molmind_branch_assistant_capture",
    default=None,
)


def _is_direct_deliverable_request(intent: AgentIntent, text: str) -> bool:
    """Whether a parsed deliverable must bypass optional chat planning.

    The intent parser has already established a concrete output type. The
    conversational LLM may refine ambiguous rank-follow-up questions, but it
    must not turn a clear CSV/run request into a chat turn because the caller
    did not repeat the session-default TopN as a literal number.
    """
    if (
        not intent.wants_tools
        or intent.mentions
        or intent.query_evidence
        or not (
            intent.want_csv
            or intent.want_pdf
            or intent.want_reserve
            or intent.want_bundle
        )
    ):
        return False
    is_rank_question, _ = ranking_question_fallback(text)
    if is_rank_question:
        return False
    if getattr(intent, "force_rescreen", False):
        return True
    # Explicit TopN on a CSV/PDF request is structural enough for offline allow
    # when the execution-gate LLM is unavailable (e.g. 「用默认配置筛选 Top20」).
    if intent.requested_top_n is not None and (
        intent.want_csv or intent.want_pdf or intent.want_bundle
    ):
        return True
    return bool(
        _DEFAULT_CONFIG_EXECUTION_RE.search(text or "")
        or _DIRECT_DELIVERABLE_RE.search(text or "")
    )


_DISCUSS_ACT_RE = re.compile(
    r"只讨论|先别跑|先别动工具|跳过执行|"
    r"先不(?:要|必)?(?:跑|执行|筛选)|本轮不(?:要|必)?(?:跑|执行)|"
    r"先 hold|hold\s*住",
    re.I,
)
_LATER_EXECUTE_ACT_RE = re.compile(
    r"帮我跑|输出\s*top|生成.{0,12}(?:csv|pdf|清单|报告)|"
    r"重新筛选|给我候选包|导出.{0,12}(?:csv|bundle|包)",
    re.I,
)


def _offline_prefer_discuss(text: str) -> bool:
    """Offline gate bias: discuss/defer cues outrank clarify when LLM is down.

    Used only as a structural fallback for compound turns such as
    「你好 + 解释 MASLD + 先别跑筛选，只讨论…」. Not a pause-phrase router for
    the online LLM path. If a later clause clearly requests execution, discuss
    loses.
    """
    raw = str(text or "").strip()
    if not raw or not _DISCUSS_ACT_RE.search(raw):
        return False
    parts = [p.strip() for p in re.split(r"[\n。；;！!？?]+", raw) if p.strip()]
    if not parts:
        parts = [raw]
    last_discuss = -1
    last_execute = -1
    for idx, part in enumerate(parts):
        if _DISCUSS_ACT_RE.search(part):
            last_discuss = idx
        if _LATER_EXECUTE_ACT_RE.search(part):
            last_execute = idx
    return last_discuss >= 0 and last_discuss >= last_execute


_PROFILE_DEFAULT_TOP_N = 10


_QUERY_EVENT_TYPES = frozenset(
    {
        "query_plan",
        "local_hit",
        "remote_start",
        "remote_end",
        "degraded",
        "identity_conflict",
        "query_summary",
    }
)
_QUERY_EVENT_FIELDS = frozenset(
    {
        "provider",
        "adapter_id",
        "query_type",
        "query_status",
        "status",
        "source",
        "count",
        "hit_count",
        "lookup_field",
        "lookup_value",
        "match_type",
        "endpoint",
        "elapsed_s",
        "allow_live",
        "force_refresh",
        "providers",
        "query_types",
        "degraded_channels",
        "message",
        "molecule_id",
        "identity",
        "evidence_ids",
        "selection_sha256_unchanged",
        "local_sources",
        "cached_remote_sources",
        "remote_provider_plan",
        "skipped_or_unsupported_sources",
        "deadline",
    }
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?:authorization|api[_-]?key|(?:access[_-]?|refresh[_-]?)?token|"
    r"credential|client[_-]?secret|private[_-]?key|secret|password|headers?)",
    re.I,
)


def _redact_query_text(value: object, *, limit: int = 1000) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+\-/=]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)\b(api[_-]?key|(?:access[_-]?|refresh[_-]?)?token|authorization|"
        r"credential|client[_-]?secret|private[_-]?key|secret|password)"
        r"\s*[:=]\s*[^\s,;&]+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)([?&](?:key|api[_-]?key|token|access[_-]?token|refresh[_-]?token|"
        r"credential|client[_-]?secret|private[_-]?key)=)[^&#\s]+",
        r"\1[REDACTED]",
        text,
    )
    return text[:limit]


def _safe_query_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_query_text(value)
    if isinstance(value, (list, tuple, set)):
        return [_safe_query_value(item, depth=depth + 1) for item in list(value)[:100]]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:100]:
            key = str(raw_key)
            if _SENSITIVE_KEY_RE.search(key):
                continue
            safe = _safe_query_value(item, depth=depth + 1)
            if safe is not None:
                out[key] = safe
        return out
    return _redact_query_text(value)


def _sanitize_query_event(event: object) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type") or "").strip()
    if event_type not in _QUERY_EVENT_TYPES:
        return None
    payload: dict[str, Any] = {"type": event_type}
    for key in _QUERY_EVENT_FIELDS:
        if key not in event:
            continue
        value = _safe_query_value(event[key])
        if value is not None:
            payload[key] = value
    return payload


def _molecule_index_from_result(result: Any) -> dict[str, list[dict[str, Any]]]:
    """Keep only lookup identity; duplicate IDs remain explicit for review."""
    molecules = list(getattr(result, "molecule_records", None) or [])
    if not molecules:
        molecules = list(getattr(result, "scored_molecules", None) or [])
    if not molecules:
        molecules = [
            *(getattr(result, "top_molecules", None) or []),
            *(getattr(result, "reserve_molecules", None) or []),
        ]
    index: dict[str, list[dict[str, Any]]] = {}
    for molecule in molecules:
        molecule_id = str(getattr(molecule, "molecule_id", "") or "").strip()
        if not molecule_id:
            continue
        ranking_signatures = []
        for hit in getattr(molecule, "evidence_hits", None) or []:
            if (
                getattr(hit, "evidence_role", "") in {"task_evidence", "risk_signal"}
                and getattr(hit, "query_type", "") in {"lipid", "tox"}
                and getattr(hit, "query_status", "") in {"hit", "exact_hit", "analogue_hit"}
                and getattr(hit, "response_sha256", "")
            ):
                ranking_signatures.append(
                    [
                        str(getattr(hit, "evidence_id", "") or ""),
                        str(getattr(hit, "response_sha256", "") or ""),
                        str(getattr(hit, "source_version", "") or ""),
                        str(getattr(hit, "adapter_version", "") or ""),
                    ]
                )
        index.setdefault(molecule_id, []).append(
            {
                "molecule_id": molecule_id,
                "inchikey": (
                    str(getattr(molecule, "inchikey", "") or "").strip() or None
                ),
                "cas": str(getattr(molecule, "cas", "") or "").strip() or None,
                "smiles": (
                    str(getattr(molecule, "smiles", "") or "").strip() or None
                ),
                "original_smiles": (
                    str(getattr(molecule, "original_smiles", "") or "").strip()
                    or None
                ),
                "standardization_steps": [
                    str(step)
                    for step in (
                        getattr(molecule, "standardization_steps", None) or []
                    )
                    if str(step)
                ],
                "ranking_evidence_signatures": ranking_signatures,
            }
        )
    return index


def _selection_guard_snapshot(result: Any) -> tuple[dict[str, Any], str]:
    """Capture the frozen ordering without deep-copying the scored library.

    ``query_evidence`` receives only ``_ReadOnlyResultSnapshot`` and the
    identity index, never these ranking objects. A shallow list snapshot is
    therefore sufficient for restoring accidental list replacement/reorder,
    while avoiding a multi-minute deepcopy of tens of thousands of records.
    """

    if result is None:
        return {}, content_sha256({})
    snapshot: dict[str, Any] = {}
    for name in ("top_molecules", "reserve_molecules", "scored_molecules"):
        if hasattr(result, name):
            snapshot[name] = list(getattr(result, name) or [])
    if hasattr(result, "selection_sha256"):
        snapshot["selection_sha256"] = str(
            getattr(result, "selection_sha256", "") or ""
        )

    def ordering(records: list[Any]) -> list[str]:
        return [
            str(
                record.get("molecule_id", "")
                if isinstance(record, dict)
                else getattr(record, "molecule_id", "")
            )
            for record in records
        ]

    digest_surface = {
        "selection_sha256": snapshot.get("selection_sha256", ""),
        "top_order": ordering(snapshot.get("top_molecules", [])),
        "reserve_order": ordering(snapshot.get("reserve_molecules", [])),
        "scored_order": ordering(snapshot.get("scored_molecules", [])),
    }
    return snapshot, content_sha256(digest_surface)


def _restore_selection_guard(result: Any, snapshot: dict[str, Any]) -> None:
    if result is None:
        return
    for name, value in snapshot.items():
        setattr(result, name, list(value) if isinstance(value, list) else value)


class _SelectionMutationAttempt(RuntimeError):
    pass


class _ReadOnlyResultSnapshot:
    """Compatibility view with no reference to mutable ranking objects."""

    __slots__ = ("selection_sha256", "molecule_records", "scored_molecules",
                 "top_molecules", "reserve_molecules", "_sealed")

    def __init__(self, selection_sha256: str):
        object.__setattr__(self, "selection_sha256", selection_sha256)
        object.__setattr__(self, "molecule_records", ())
        object.__setattr__(self, "scored_molecules", ())
        object.__setattr__(self, "top_molecules", ())
        object.__setattr__(self, "reserve_molecules", ())
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise _SelectionMutationAttempt(
                f"query_evidence attempted to mutate read-only result field: {name}"
            )
        object.__setattr__(self, name, value)


class SessionBusyError(RuntimeError):
    """Raised when a session-scoped mutation conflicts with an active Run."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = dict(payload)
        super().__init__(str(payload.get("message") or "当前会话仍在执行"))


class TurnQueueFullError(RuntimeError):
    """Raised when the three normal waiting slots are already occupied."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = dict(payload)
        super().__init__(str(payload.get("message") or "当前会话排队已满"))


class RunInterrupted(RuntimeError):
    """Internal cooperative unwind used after a guidance request."""


class AgentRuntime:
    def __init__(self, store: PostgresRunStore | None = None) -> None:
        self.store = store or STORE
        self.registry = get_registry()
        self.task_router = TaskRouter(self.registry)
        self.observation_validator = ObservationValidator(self.registry)
        self.scp = SCPRegistryManager(self.registry, SCPCatalog())
        self.scp_jobs = SCPJobManager(max_workers=2)
        # One mutable AgentSession is shared by all HTTP streams for its id.
        # A session lock preserves user-turn order while allowing unrelated
        # sessions to use the worker pool concurrently.
        self._session_locks: dict[str, threading.Lock] = {}
        self._session_locks_guard = threading.Lock()
        # Serializes short Run reservations and session mutations. Long-running
        # work does not hold this guard, so conflicting HTTP calls fail fast.
        self._session_state_guard = threading.RLock()
        # Independent branches within one user turn (for example an R0 lookup
        # plus a normal question) may overlap. Session mutation remains on the
        # owning turn thread; workers are used only for read-only LLM replies.
        self._branch_executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="agent-branch",
        )
        self._governance = ToolGovernance(self.registry)
        self._run_controllers: dict[str, RunController] = {}
        self._run_controllers_guard = threading.Lock()

    def create_session(
        self,
        *,
        profile_id: str = "competition_masld",
        client_id: str = "",
    ) -> AgentSession:
        return self.store.create(profile_id=profile_id, client_id=client_id)

    def get_session(self, session_id: str) -> AgentSession | None:
        session = self.store.get(session_id)
        if session is not None:
            self.scp.restore_session(session)
            ensure_session_last_result(session)
        return session

    def _session_lock(self, session_id: str) -> threading.Lock:
        with self._session_locks_guard:
            return self._session_locks.setdefault(session_id, threading.Lock())

    @contextmanager
    def _state_lock(self, session_id: str):
        """Serialize short session mutations across threads and API processes."""
        with self._session_state_guard:
            with self.store.mutation_lock(session_id):
                yield

    @staticmethod
    def _run_is_active(run: dict[str, Any] | None) -> bool:
        return bool(
            isinstance(run, dict)
            and str(run.get("status") or "")
            in {"queued", "running", "cancel_requested"}
        )

    def session_busy_payload(
        self,
        session: AgentSession,
        *,
        operation: str,
    ) -> dict[str, Any] | None:
        run = session.active_run
        if not self._run_is_active(run):
            return None
        return {
            "code": "session_busy",
            "message": "当前会话仍在执行",
            "session_id": session.session_id,
            "run_id": str(run.get("run_id") or ""),
            "run_status": str(run.get("status") or "running"),
            "blocked_operation": operation,
            "retryable": True,
        }

    def request_external_run_interrupt(
        self,
        session_id: str,
        run_id: str,
        *,
        reason: str,
    ) -> bool:
        """Deliver a distributed cancellation request to the owning worker."""
        with self._run_controllers_guard:
            controller = self._run_controllers.get(session_id)
        if controller is None or controller.run_id != run_id:
            return False
        controller.request_interrupt(reason=reason)
        return True

    def interrupt_session_run(
        self,
        session_id: str,
        run_id: str | None = None,
        *,
        reason: str = "user_stop",
    ) -> dict[str, Any]:
        """Request hard interrupt of the session's active Run (no guidance turn)."""
        reason = str(reason or "user_stop").strip() or "user_stop"
        with self._state_lock(session_id):
            session = self.get_session(session_id)
            if session is None:
                raise KeyError("会话不存在")
            active = dict(session.active_run or {})
            if not self._run_is_active(active):
                raise ValueError("当前没有正在执行的任务")
            active_run_id = str(active.get("run_id") or "")
            if run_id and active_run_id and str(run_id) != active_run_id:
                raise ValueError("run_id 与当前任务不匹配")
            active["status"] = "cancel_requested"
            active["interrupt_reason"] = reason
            active["heartbeat_at"] = utc_now()
            session.active_run = active
            controller = None
            with self._run_controllers_guard:
                controller = self._run_controllers.get(session.session_id)
            if controller and (
                not active_run_id or controller.run_id == active_run_id
            ):
                controller.request_interrupt(reason=reason)
                session.agent_run_state = controller.snapshot()
            for checkpoint in session.tool_checkpoints:
                if (
                    checkpoint.get("run_id") == active_run_id
                    and checkpoint.get("status") == "running"
                ):
                    checkpoint["status"] = "interrupted"
                    checkpoint["ended_at"] = utc_now()
                    checkpoint["retryable"] = True
                    checkpoint["interrupt_reason"] = reason
            cancelled_jobs = self.scp_jobs.cancel_for_run(
                session_id=session.session_id,
                run_id=active_run_id,
                reason=reason,
            )
            mechanism_job_id = str(session.last_mechanism_job_id or "")
            mechanism_job = get_job(mechanism_job_id) if mechanism_job_id else None
            if (
                mechanism_job
                and mechanism_job.get("agent_run_id") == active_run_id
                and cancel_job(mechanism_job_id, reason=reason)
            ):
                cancelled_jobs.append(mechanism_job_id)
            self.store.persist(session)
            return {
                "interrupted": True,
                "run_id": active_run_id,
                "status": "cancel_requested",
                "reason": reason,
                "cancelled_background_jobs": cancelled_jobs,
            }

    def reserve_session_run(
        self,
        session_id: str,
        text: str,
        *,
        top_n: int | None = None,
        attachment_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Atomically reserve the one active Run allowed for a Session."""
        with self._state_lock(session_id):
            session = self.get_session(session_id)
            if session is None:
                raise KeyError("会话不存在")
            busy = self.session_busy_payload(session, operation="send_message")
            if busy:
                raise SessionBusyError(busy)
            ids = [str(item) for item in (attachment_ids or []) if str(item)]
            for attachment_id in ids:
                metadata = session.staged_attachments.get(attachment_id)
                if not isinstance(metadata, dict) or metadata.get("state") not in {
                    "draft",
                    "queued",
                }:
                    raise ValueError(f"暂存附件不存在或不可用：{attachment_id}")
            for attachment_id in ids:
                session.staged_attachments[attachment_id]["state"] = "queued"
            session.active_run = self._new_active_run(
                session,
                text=text,
                top_n=top_n,
                attachment_ids=ids,
            )
            self.store.persist(session)
            return copy.deepcopy(session.active_run)

    @staticmethod
    def _summaries_for_attachment_ids(
        session: AgentSession,
        attachment_ids: list[str] | None,
    ) -> list[dict[str, Any]]:
        """Lightweight filename/kind chips from staged metadata (no blob read)."""
        summaries: list[dict[str, Any]] = []
        staged = session.staged_attachments if session else {}
        for raw_id in attachment_ids or []:
            attachment_id = str(raw_id or "")
            if not attachment_id:
                continue
            meta = staged.get(attachment_id) if isinstance(staged, dict) else None
            if not isinstance(meta, dict):
                continue
            summaries.append(
                {
                    "attachment_id": attachment_id,
                    "filename": str(meta.get("filename") or ""),
                    "kind": str(meta.get("kind") or ""),
                    "size": int(meta.get("size") or 0),
                    "media_type": str(meta.get("media_type") or ""),
                }
            )
        return summaries

    def _new_active_run(
        self,
        session: AgentSession,
        *,
        text: str,
        top_n: int | None = None,
        turn_id: str = "",
        kind: str = "message",
        attachment_ids: list[str] | None = None,
        parent_run_id: str = "",
        resume_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run_id = f"agent-{uuid.uuid4().hex[:12]}"
        ids = list(attachment_ids or [])
        active = {
                "run_id": run_id,
                "turn_id": turn_id or run_id,
                "kind": kind,
                "parent_run_id": parent_run_id,
                "status": "queued",
                "started_at": utc_now(),
                "heartbeat_at": utc_now(),
                "ended_at": "",
                "last_event_seq": session.event_seq,
                "session_revision": session.revision,
                "input": {
                    "text": str(text),
                    "sdf_filename": session.sdf_filename,
                    "sdf_sha256": (
                        hashlib.sha256(session.sdf_bytes).hexdigest()
                        if session.sdf_bytes
                        else ""
                    ),
                    "top_n": int(top_n if top_n is not None else session.top_n),
                    "profile_id": session.profile_id,
                    "catalog_ids": list(session.installed_catalog),
                    "attachment_ids": ids,
                },
                "resume_context": copy.deepcopy(resume_context),
            }
        # Surface chips immediately so /turns/next and live UI can paint the ask
        # before the worker loads blob content into rich summaries.
        summaries = self._summaries_for_attachment_ids(session, ids)
        if summaries:
            active["attachment_summaries"] = summaries
        return active

    @staticmethod
    def _normal_queue_size(session: AgentSession) -> int:
        return sum(1 for item in session.pending_turns if item.get("kind") != "guidance")

    def submit_session_turn(
        self,
        session_id: str,
        text: str,
        *,
        mode: str = "auto",
        attachment_ids: list[str] | None = None,
        idempotency_key: str = "",
        top_n: int | None = None,
    ) -> dict[str, Any]:
        """Start immediately or durably enqueue one immutable user Turn."""
        clean_text = str(text or "").strip()
        ids = [str(item) for item in (attachment_ids or []) if str(item)]
        with self._state_lock(session_id):
            session = self.get_session(session_id)
            if session is None:
                raise KeyError("会话不存在")
            if idempotency_key:
                active = session.active_run or {}
                if active.get("idempotency_key") == idempotency_key:
                    return {
                        "disposition": "started",
                        "duplicate": True,
                        **copy.deepcopy(active),
                    }
                duplicate = next(
                    (item for item in session.pending_turns if item.get("idempotency_key") == idempotency_key),
                    None,
                )
                if duplicate:
                    return {
                        "disposition": "queued",
                        "duplicate": True,
                        "queue_position": session.pending_turns.index(duplicate) + 1,
                        **copy.deepcopy(duplicate),
                    }
            for attachment_id in ids:
                metadata = session.staged_attachments.get(attachment_id)
                if not isinstance(metadata, dict) or metadata.get("state") != "draft":
                    raise ValueError(f"暂存附件不存在或不可用：{attachment_id}")
            if mode == "guidance":
                return self._request_guidance_locked(
                    session,
                    text=clean_text,
                    attachment_ids=ids,
                    idempotency_key=idempotency_key,
                    top_n=top_n,
                )
            # mode=queue always enqueues — even when the durable Run is already
            # terminal. The browser may still be draining the previous turn's
            # streaming UI and only promotes via POST /turns/next after settle.
            if mode != "queue" and not self._run_is_active(session.active_run):
                active = self._new_active_run(
                    session,
                    text=clean_text,
                    top_n=top_n,
                    attachment_ids=ids,
                )
                active["idempotency_key"] = idempotency_key
                session.active_run = active
                for attachment_id in ids:
                    session.staged_attachments[attachment_id]["state"] = "queued"
                self.store.persist(session)
                return {"disposition": "started", **copy.deepcopy(active)}
            if mode == "run_now":
                busy = self.session_busy_payload(session, operation="send_message") or {}
                raise SessionBusyError(busy)
            if self._normal_queue_size(session) >= 3:
                raise TurnQueueFullError(
                    {
                        "code": "turn_queue_full",
                        "message": "当前会话最多排队 3 轮",
                        "session_id": session_id,
                        "queue_limit": 3,
                    }
                )
            turn_id = f"turn-{uuid.uuid4().hex[:12]}"
            item = {
                "turn_id": turn_id,
                "kind": "message",
                "status": "queued",
                "text": clean_text,
                "attachment_ids": ids,
                "top_n": top_n,
                "idempotency_key": idempotency_key,
                "created_at": utc_now(),
            }
            session.pending_turns.append(item)
            for attachment_id in ids:
                session.staged_attachments[attachment_id]["state"] = "queued"
            self.store.persist(session)
            return {
                "disposition": "queued",
                "queue_position": self._normal_queue_size(session),
                **copy.deepcopy(item),
            }

    def _request_guidance_locked(
        self,
        session: AgentSession,
        *,
        text: str,
        attachment_ids: list[str],
        idempotency_key: str,
        top_n: int | None,
    ) -> dict[str, Any]:
        active = session.active_run or {}
        if not self._run_is_active(active):
            replacement = self._new_active_run(
                session,
                text=text,
                top_n=top_n,
                attachment_ids=attachment_ids,
            )
            replacement["idempotency_key"] = idempotency_key
            session.active_run = replacement
            self.store.persist(session)
            return {"disposition": "started", **copy.deepcopy(replacement)}
        if any(item.get("kind") == "guidance" for item in session.pending_turns):
            raise SessionBusyError(
                {
                    "code": "guidance_pending",
                    "message": "上一条指引正在等待当前步骤停止",
                    "session_id": session.session_id,
                    "run_id": active.get("run_id") or "",
                    "retryable": True,
                }
            )
        guidance_id = f"guide-{uuid.uuid4().hex[:12]}"
        controller = None
        with self._run_controllers_guard:
            controller = self._run_controllers.get(session.session_id)
        controller_snapshot = controller.snapshot() if controller else None
        resume = {
            "original_goal": str((active.get("input") or {}).get("text") or ""),
            "latest_guidance": text,
            "parent_run_id": str(active.get("run_id") or ""),
            "completed_plan": copy.deepcopy(session.active_plan),
            "working_memory": copy.deepcopy(session.working_memory[-6:]),
            "run_controller": controller_snapshot,
            "artifact_ids": list(session.artifacts.keys()),
        }
        session.resume_context = resume
        active["status"] = "cancel_requested"
        active["guidance_id"] = guidance_id
        active["interrupt_reason"] = "user_guidance"
        active["heartbeat_at"] = utc_now()
        session.active_run = active
        if controller:
            controller.request_interrupt(reason="user_guidance", guidance_id=guidance_id)
            session.agent_run_state = controller.snapshot()
        for checkpoint in session.tool_checkpoints:
            if (
                checkpoint.get("run_id") == str(active.get("run_id") or "")
                and checkpoint.get("status") == "running"
            ):
                checkpoint["status"] = "interrupted"
                checkpoint["ended_at"] = utc_now()
                checkpoint["retryable"] = True
                checkpoint["interrupt_reason"] = "user_guidance"
        resume["completed_tool_checkpoints"] = [
            copy.deepcopy(checkpoint)
            for checkpoint in session.tool_checkpoints
            if checkpoint.get("run_id") == str(active.get("run_id") or "")
            and checkpoint.get("status") == "succeeded"
        ]
        resume["pending_tool_checkpoints"] = [
            copy.deepcopy(checkpoint)
            for checkpoint in session.tool_checkpoints
            if checkpoint.get("run_id") == str(active.get("run_id") or "")
            and checkpoint.get("status") in {"running", "interrupted", "failed"}
        ]
        cancelled_jobs = self.scp_jobs.cancel_for_run(
            session_id=session.session_id,
            run_id=str(active.get("run_id") or ""),
            reason="user_guidance",
        )
        mechanism_job_id = str(session.last_mechanism_job_id or "")
        mechanism_job = get_job(mechanism_job_id) if mechanism_job_id else None
        if (
            mechanism_job
            and mechanism_job.get("agent_run_id") == str(active.get("run_id") or "")
            and cancel_job(mechanism_job_id, reason="user_guidance")
        ):
            cancelled_jobs.append(mechanism_job_id)
        resume["cancelled_background_jobs"] = cancelled_jobs
        item = {
            "turn_id": f"turn-{uuid.uuid4().hex[:12]}",
            "guidance_id": guidance_id,
            "kind": "guidance",
            "status": "queued",
            "text": text,
            "attachment_ids": attachment_ids,
            "top_n": top_n,
            "idempotency_key": idempotency_key,
            "parent_run_id": str(active.get("run_id") or ""),
            "resume_context": resume,
            "created_at": utc_now(),
        }
        session.pending_turns.insert(0, item)
        for attachment_id in attachment_ids:
            session.staged_attachments[attachment_id]["state"] = "queued"
        self.store.persist(session)
        return {
            "disposition": "guidance",
            "queue_position": 0,
            **copy.deepcopy(item),
        }

    def activate_next_queued_turn(self, session_id: str) -> dict[str, Any] | None:
        """Atomically promote the queue head after the preceding Run is terminal."""
        with self._state_lock(session_id):
            session = self.get_session(session_id)
            if session is None or self._run_is_active(session.active_run):
                return None
            if not session.pending_turns:
                return None
            item = session.pending_turns.pop(0)
            resume_context = copy.deepcopy(item.get("resume_context") or {})
            if item.get("kind") == "guidance":
                resume_context.update(
                    {
                        "completed_plan": copy.deepcopy(session.active_plan),
                        "working_memory": copy.deepcopy(session.working_memory[-6:]),
                        "run_controller": copy.deepcopy(session.agent_run_state),
                        "artifact_ids": list(session.artifacts.keys()),
                    }
                )
                session.resume_context = resume_context
            original = str(resume_context.get("original_goal") or "").strip()
            execution_text = str(item.get("text") or "")
            if item.get("kind") == "guidance" and original:
                execution_text = f"原任务：{original}\n用户补充指引：{execution_text}"
            active = self._new_active_run(
                session,
                text=execution_text,
                top_n=item.get("top_n"),
                turn_id=str(item.get("turn_id") or ""),
                kind=str(item.get("kind") or "message"),
                attachment_ids=list(item.get("attachment_ids") or []),
                parent_run_id=str(item.get("parent_run_id") or ""),
                resume_context=resume_context,
            )
            active["display_text"] = str(item.get("text") or "")
            active["idempotency_key"] = str(item.get("idempotency_key") or "")
            if item.get("kind") == "guidance" and item.get("parent_run_id"):
                active["retry_of_run_id"] = str(item.get("parent_run_id") or "")
            session.active_run = active
            self.store.persist(session)
            return copy.deepcopy(active)

    def cancel_queued_turn(self, session_id: str, turn_id: str) -> bool:
        with self._state_lock(session_id):
            session = self.get_session(session_id)
            if session is None:
                raise KeyError("会话不存在")
            index = next(
                (i for i, item in enumerate(session.pending_turns) if item.get("turn_id") == turn_id),
                -1,
            )
            if index < 0:
                return False
            item = session.pending_turns.pop(index)
            for attachment_id in item.get("attachment_ids") or []:
                metadata = session.staged_attachments.get(str(attachment_id))
                if isinstance(metadata, dict):
                    metadata["state"] = "draft"
            self.store.persist(session)
            return True

    def update_queued_turn(
        self,
        session_id: str,
        turn_id: str,
        *,
        text: str | None = None,
        attachment_ids: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Edit a queued Turn without changing the currently executing Run."""
        with self._state_lock(session_id):
            session = self.get_session(session_id)
            if session is None:
                raise KeyError("会话不存在")
            item = next(
                (entry for entry in session.pending_turns if entry.get("turn_id") == turn_id),
                None,
            )
            if item is None:
                return None
            if text is not None:
                clean = str(text).strip()
                if not clean:
                    raise ValueError("消息不能为空")
                item["text"] = clean
                if item.get("kind") == "guidance":
                    item.setdefault("resume_context", {})["latest_guidance"] = clean
                    session.resume_context["latest_guidance"] = clean
            if attachment_ids is not None:
                ids = list(dict.fromkeys(str(value) for value in attachment_ids if str(value)))
                old_ids = {str(value) for value in item.get("attachment_ids") or []}
                for attachment_id in ids:
                    metadata = session.staged_attachments.get(attachment_id)
                    allowed_states = {"draft", "queued"} if attachment_id in old_ids else {"draft"}
                    if not isinstance(metadata, dict) or metadata.get("state") not in allowed_states:
                        raise ValueError(f"暂存附件不存在或不可用：{attachment_id}")
                for attachment_id in old_ids - set(ids):
                    metadata = session.staged_attachments.get(attachment_id)
                    if isinstance(metadata, dict):
                        metadata["state"] = "draft"
                for attachment_id in ids:
                    session.staged_attachments[attachment_id]["state"] = "queued"
                item["attachment_ids"] = ids
            item["updated_at"] = utc_now()
            self.store.persist(session)
            return copy.deepcopy(item)

    def reorder_queued_turns(self, session_id: str, turn_ids: list[str]) -> list[dict[str, Any]]:
        """Reorder normal Turns while keeping a pending guidance Turn first."""
        with self._state_lock(session_id):
            session = self.get_session(session_id)
            if session is None:
                raise KeyError("会话不存在")
            guidance = [item for item in session.pending_turns if item.get("kind") == "guidance"]
            normal = [item for item in session.pending_turns if item.get("kind") != "guidance"]
            current_ids = [str(item.get("turn_id") or "") for item in normal]
            if len(turn_ids) != len(set(turn_ids)) or set(turn_ids) != set(current_ids):
                raise ValueError("turn_ids 必须完整且不能重复")
            by_id = {str(item.get("turn_id") or ""): item for item in normal}
            session.pending_turns = guidance + [by_id[turn_id] for turn_id in turn_ids]
            self.store.persist(session)
            return copy.deepcopy(session.pending_turns)

    def retry_session_run(self, session_id: str, run_id: str) -> dict[str, Any]:
        """Create a new Run linked to a terminal Run and its tool checkpoints."""
        with self._state_lock(session_id):
            session = self.get_session(session_id)
            if session is None:
                raise KeyError("会话不存在")
            if self._run_is_active(session.active_run):
                raise SessionBusyError(
                    self.session_busy_payload(session, operation="retry_run") or {}
                )
            candidates = list(session.agent_run_history)
            if isinstance(session.active_run, dict):
                candidates.append(session.active_run)
            source = next(
                (item for item in reversed(candidates) if str(item.get("run_id") or "") == run_id),
                None,
            )
            if source is None or str(source.get("status") or "") not in {
                "succeeded", "failed", "cancelled", "interrupted"
            }:
                raise ValueError("只能重试已结束的 Run")
            source_input = dict(source.get("input") or {})
            retry = self._new_active_run(
                session,
                text=str(source_input.get("text") or ""),
                top_n=source_input.get("top_n"),
                kind="retry",
                attachment_ids=list(source_input.get("attachment_ids") or []),
                parent_run_id=run_id,
                resume_context={
                    "retry_of_run_id": run_id,
                    "completed_tool_checkpoints": [
                        copy.deepcopy(item)
                        for item in session.tool_checkpoints
                        if item.get("run_id") == run_id and item.get("status") == "succeeded"
                    ],
                    "pending_tool_checkpoints": [
                        copy.deepcopy(item)
                        for item in session.tool_checkpoints
                        if item.get("run_id") == run_id
                        and item.get("status") in {"running", "interrupted", "failed"}
                    ],
                },
            )
            retry["retry_of_run_id"] = run_id
            session.active_run = retry
            self.store.persist(session)
            return copy.deepcopy(retry)

    def handle_reserved_session_message(
        self,
        session_id: str,
        run_id: str,
        text: str,
        *,
        top_n: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Execute a previously reserved Run and always persist a terminal state."""
        with self._session_lock(session_id):
            session = self.get_session(session_id)
            if session is None:
                raise KeyError("会话不存在")
            active = session.active_run or {}
            if str(active.get("run_id") or "") != run_id:
                raise RuntimeError("活动 Run 已变化")
            attachment_summaries: list[dict[str, Any]] = []
            for attachment_id in (active.get("input") or {}).get("attachment_ids") or []:
                staged = self.store.read_staged_attachment(session, str(attachment_id))
                if staged is None:
                    continue
                metadata, content = staged
                aid = str(attachment_id)
                if str(metadata.get("kind") or "") == "sdf" or str(
                    metadata.get("filename") or ""
                ).lower().endswith(".sdf"):
                    session.sdf_bytes = content
                    session.sdf_filename = str(metadata.get("filename") or "library.sdf")
                    session.sdf_ui_pending = True
                    self.store.save_sdf(session)
                    # Keep SDF on the ask chip rail (previously only non-SDF
                    # summaries were stored, so queued SDF turns rendered blank).
                    attachment_summaries.append(
                        {
                            "attachment_id": aid,
                            "filename": session.sdf_filename,
                            "kind": "sdf",
                            "size": int(metadata.get("size") or len(content) or 0),
                            "media_type": str(
                                metadata.get("media_type") or "chemical/x-mdl-sdfile"
                            ),
                            "note": "化合物库 SDF",
                        }
                    )
                else:
                    summary = summarize_attachment_for_context(metadata, content)
                    summary["attachment_id"] = aid
                    attachment_summaries.append(summary)
                session.staged_attachments[aid]["state"] = "active"
            if attachment_summaries:
                active["attachment_summaries"] = attachment_summaries
            # Guidance may arrive after reservation but before the worker has
            # created its controller. Preserve that request so the first
            # checkpoint unwinds instead of accidentally starting the old goal.
            if str(active.get("status") or "") != "cancel_requested":
                active["status"] = "running"
            active["heartbeat_at"] = utc_now()
            session.active_run = active
            self.store.persist(session)
            if top_n is not None:
                session.top_n = top_n
            saw_done = False
            try:
                for event in self.handle_message(session, text, run_id=run_id):
                    controller = self._run_controller(session)
                    if controller.interruption_requested and event.get("type") != "tool_end":
                        raise RunInterrupted(controller.interrupt_reason or "user_guidance")
                    saw_done = saw_done or event.get("type") == "done"
                    yield event
                    if controller.interruption_requested:
                        raise RunInterrupted(controller.interrupt_reason or "user_guidance")
            except RunInterrupted:
                controller = self._run_controller(session)
                interrupt_reason = str(
                    controller.interrupt_reason
                    or (session.active_run or {}).get("interrupt_reason")
                    or "user_guidance"
                )
                yield self._emit(
                    session,
                    {
                        "type": "run_interrupted",
                        "detail": (
                            "已根据用户指引停止当前任务，正在重新规划。"
                            if interrupt_reason == "user_guidance"
                            else "已停止当前任务。"
                        ),
                        "run_id": run_id,
                        "guidance_id": controller.guidance_id,
                        "interrupt_reason": interrupt_reason,
                    },
                )
                yield self._emit(
                    session,
                    {"type": "done", "run_id": run_id, "status": "interrupted"},
                )
                saw_done = True
            except CallCancelled as exc:
                controller = self._run_controller(session)
                if not controller.interruption_requested:
                    controller.request_interrupt(
                        reason=str(
                            (session.active_run or {}).get("interrupt_reason")
                            or "user_stop"
                        )
                    )
                interrupt_reason = str(
                    controller.interrupt_reason
                    or (session.active_run or {}).get("interrupt_reason")
                    or "user_stop"
                )
                yield self._emit(
                    session,
                    {
                        "type": "run_interrupted",
                        "detail": (
                            "已根据用户指引停止当前任务，正在重新规划。"
                            if interrupt_reason == "user_guidance"
                            else "已停止当前任务。"
                        ),
                        "run_id": run_id,
                        "guidance_id": controller.guidance_id,
                        "interrupt_reason": interrupt_reason,
                    },
                )
                yield self._emit(
                    session,
                    {"type": "done", "run_id": run_id, "status": "interrupted"},
                )
                saw_done = True
                _ = exc
            except Exception as exc:  # noqa: BLE001 - turn is closed durably here
                controller = self._run_controller(session)
                controller.stop("unhandled_exception", status="failed")
                yield self._emit(
                    session,
                    {"type": "error", "detail": str(exc), "run_id": run_id},
                )
                yield self._emit(
                    session,
                    {"type": "done", "run_id": run_id, "status": "failed"},
                )
                saw_done = True
            finally:
                consumed_changed = False
                for attachment_id in (active.get("input") or {}).get("attachment_ids") or []:
                    metadata = session.staged_attachments.get(str(attachment_id))
                    if isinstance(metadata, dict):
                        metadata["state"] = "consumed"
                        metadata["consumed_at"] = utc_now()
                        consumed_changed = True
                current = session.active_run or {}
                if str(current.get("run_id") or "") == run_id and self._run_is_active(current):
                    # If the turn already emitted done but active_run was not
                    # closed (run_id mismatch / reload race), do not force
                    # failed over a completed transcript.
                    if not saw_done:
                        current["status"] = "interrupted"
                    else:
                        current["status"] = str(current.get("status") or "succeeded")
                        if current["status"] in {"queued", "running", "cancel_requested"}:
                            current["status"] = "succeeded"
                    current["ended_at"] = utc_now()
                    current["heartbeat_at"] = utc_now()
                    session.active_run = current
                    self.store.persist(session)
                elif consumed_changed:
                    self.store.persist(session)

    def handle_session_message(
        self,
        session_id: str,
        text: str,
        *,
        top_n: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Run one user turn after earlier turns of the same session finish."""
        with self._session_lock(session_id):
            session = self.get_session(session_id)
            if session is None:
                raise KeyError("会话不存在")
            if top_n is not None:
                session.top_n = top_n
            yield from self.handle_message(session, text)

    def _mutate_idle_session(
        self,
        session_id: str,
        operation: str,
        mutation,
    ):
        with self._state_lock(session_id):
            session = self.get_session(session_id)
            if session is None:
                raise KeyError("会话不存在")
            busy = self.session_busy_payload(session, operation=operation)
            if busy:
                raise SessionBusyError(busy)
            with self._session_lock(session_id):
                result = mutation(session)
                session.revision += 1
                self.store.persist(session)
                return result if result is not None else session

    def attach_session_sdf(self, session_id: str, *, filename: str, content: bytes) -> AgentSession:
        return self._mutate_idle_session(
            session_id,
            "attach_sdf",
            lambda session: self.attach_sdf(session, filename=filename, content=content),
        )

    def detach_session_sdf(self, session_id: str) -> AgentSession:
        return self._mutate_idle_session(
            session_id,
            "detach_sdf",
            self.detach_sdf,
        )

    def install_session_catalog(self, session_id: str, plugin_id: str) -> AgentSession:
        return self._mutate_idle_session(
            session_id,
            "catalog_install",
            lambda session: self.install_catalog_plugin(session, plugin_id),
        )

    def uninstall_session_catalog(self, session_id: str, plugin_id: str) -> AgentSession:
        return self._mutate_idle_session(
            session_id,
            "catalog_uninstall",
            lambda session: self.uninstall_catalog_plugin(session, plugin_id),
        )

    def delete_session_when_idle(self, session_id: str) -> bool:
        with self._state_lock(session_id):
            session = self.get_session(session_id)
            if session is None:
                return False
            busy = self.session_busy_payload(session, operation="delete_session")
            if busy:
                raise SessionBusyError(busy)
            with self._session_lock(session_id):
                return self.store.delete_session(session_id)

    def clear_sessions_when_idle(self, *, client_id: str) -> int:
        with self._session_state_guard:
            sessions = self.store.list_sessions(limit=10_000, client_id=client_id)
            active: list[dict[str, Any]] = []
            for item in sessions:
                session = self.get_session(str(item.get("session_id") or ""))
                if session and self._run_is_active(session.active_run):
                    active.append(copy.deepcopy(session.active_run or {}))
            if active:
                raise SessionBusyError(
                    {
                        "code": "sessions_busy",
                        "message": f"有 {len(active)} 个任务正在执行",
                        "blocked_operation": "clear_sessions",
                        "active_runs": active,
                        "retryable": True,
                    }
                )
            return self.store.clear_sessions(client_id=client_id)

    def attach_sdf(self, session: AgentSession, *, filename: str, content: bytes) -> None:
        new_sha = hashlib.sha256(content or b"").hexdigest()
        previous = session.sdf_bytes or b""
        previous_sha = hashlib.sha256(previous).hexdigest() if previous else ""
        content_changed = new_sha != previous_sha
        session.sdf_bytes = content
        session.sdf_filename = filename or "library.sdf"
        session.sdf_ui_pending = True
        # Re-attaching the identical library must not wipe a durable freeze;
        # only a changed compound library invalidates the ranking snapshot.
        if content_changed:
            session.last_result = None
            session.frozen_ranking = None
            session.last_run_id = ""
            session.last_selection_sha256 = ""
            session.last_molecule_index = {}
            session.last_mechanism_job_id = ""
            session.active_plan = None
        self.store.save_sdf(session)

    def detach_sdf(self, session: AgentSession) -> None:
        session.last_result = None
        session.frozen_ranking = None
        session.last_run_id = ""
        session.last_selection_sha256 = ""
        session.last_config_hash = ""
        session.last_input_sha256 = ""
        session.last_molecule_index = {}
        session.last_mechanism_job_id = ""
        session.active_plan = None
        session.sdf_ui_pending = False
        self.store.clear_sdf(session)

    def install_catalog_plugin(self, session: AgentSession, plugin_id: str) -> None:
        catalog_ids = {p.plugin_id for p in self.registry.list_catalog()}
        if plugin_id not in catalog_ids:
            raise KeyError(f"Catalog 中不存在: {plugin_id}")
        if plugin_id not in session.installed_catalog:
            session.installed_catalog.append(plugin_id)
            self.store.persist(session)

    def uninstall_catalog_plugin(self, session: AgentSession, plugin_id: str) -> None:
        session.installed_catalog = [p for p in session.installed_catalog if p != plugin_id]
        self.store.persist(session)

    def rename_session(self, session: AgentSession, title: str) -> None:
        self.store.set_title(session, title)

    def delete_session(self, session_id: str) -> bool:
        return self.store.delete_session(session_id)

    def clear_sessions(self, *, client_id: str | None = None) -> int:
        return self.store.clear_sessions(client_id=client_id)

    def settings_view(self, session: AgentSession | None = None) -> dict[str, Any]:
        installed = set(session.installed_catalog) if session else set()
        profile_id = session.profile_id if session else "competition_masld"
        view = self.registry.settings_view(
            profile_id=profile_id,
            installed_catalog=installed,
        )
        view["scp_skills"] = list((session.installed_scp_skills if session else {}).values())
        installed_scp = session.installed_scp_skills if session else {}
        installed_scp_tools = sorted(
            {
                str(tool_id)
                for state in installed_scp.values()
                for tool_id in state.get("tools", [])
            }
        )
        for plugin in view.get("plugins", []):
            if plugin.get("plugin_id") == "scp-hub":
                plugin["tools"] = installed_scp_tools
        view["scp_catalog"] = [
            {
                **item,
                "id": item.get("skill_id"),
                "plugin_id": "scp-hub",
                "installed": item.get("skill_id") in installed_scp,
                "enabled": bool(installed_scp.get(str(item.get("skill_id")), {}).get("enabled")),
                "credential_status": installed_scp.get(str(item.get("skill_id")), {}).get("credential_status", "unknown"),
                "tools": [
                    str(tool_name)
                    for server in item.get("servers", [])
                    for tool_name in server.get("tools", [])
                ],
            }
            for item in self.scp.catalog.list()
        ]
        return view

    def install_scp_skill(self, session_id: str, skill_id: str) -> AgentSession:
        return self._mutate_idle_session(session_id, "scp_install", lambda session: self.scp.install(session, skill_id) and session)

    def set_scp_skill_enabled(self, session_id: str, skill_id: str, enabled: bool) -> AgentSession:
        return self._mutate_idle_session(session_id, "scp_enable", lambda session: self.scp.set_enabled(session, skill_id, enabled) and session)

    def uninstall_scp_skill(self, session_id: str, skill_id: str) -> AgentSession:
        return self._mutate_idle_session(session_id, "scp_uninstall", lambda session: self.scp.uninstall(session, skill_id) or session)

    def grant_tool_approval(
        self,
        session: AgentSession,
        *,
        tool_id: str,
        args: dict[str, Any],
        ttl_sec: int = 600,
    ) -> dict[str, Any]:
        tool = self.registry.tools.get(tool_id)
        if tool is None:
            raise KeyError(f"工具未注册：{tool_id}")
        profile = self.registry.get_profile(session.profile_id)
        from agent.runtime.governance import approval_scope

        scope = approval_scope(tool, dict(args or {}), profile.policy)
        if not scope:
            raise ValueError(f"工具 {tool_id} 不需要审批")
        record = grant_approval(
            session,
            tool_id=tool_id,
            args=dict(args or {}),
            scope=scope,
            ttl_sec=ttl_sec,
        )
        self.store.persist(session)
        return record

    def _begin_agent_turn(
        self,
        session: AgentSession,
        *,
        run_id: str | None = None,
    ) -> RunController:
        profile = self.registry.get_profile(session.profile_id)
        controller = RunController(
            RunBudget.from_mapping(profile.budgets),
            run_id=run_id,
        )
        active = session.active_run or {}
        if (
            str(active.get("run_id") or "") == controller.run_id
            and str(active.get("status") or "") == "cancel_requested"
        ):
            controller.request_interrupt(
                reason=str(active.get("interrupt_reason") or "user_guidance"),
                guidance_id=str(active.get("guidance_id") or ""),
            )
        with self._run_controllers_guard:
            self._run_controllers[session.session_id] = controller
        session.agent_run_state = controller.snapshot()
        return controller

    def _run_controller(self, session: AgentSession) -> RunController:
        with self._run_controllers_guard:
            controller = self._run_controllers.get(session.session_id)
        if controller is None:
            controller = self._begin_agent_turn(session)
        return controller

    def _task_id_for_tool(self, session: AgentSession, tool_id: str) -> str:
        active = session.active_plan
        if not isinstance(active, dict):
            return ""
        for step in active.get("steps") or []:
            if step.get("tool") == tool_id and step.get("status") == "pending":
                return str(step.get("task_id") or "")
        return ""

    def _authorize_tool_call(
        self,
        session: AgentSession,
        tool_id: str,
        args: dict[str, Any],
        *,
        confirmed_scopes: set[str] | None = None,
    ):
        controller = self._run_controller(session)
        task_id = self._task_id_for_tool(session, tool_id)
        active = session.active_plan
        if task_id and isinstance(active, dict):
            statuses = {
                str(step.get("task_id") or ""): str(step.get("status") or "")
                for step in active.get("steps") or []
            }
            target = next(
                (
                    step
                    for step in active.get("steps") or []
                    if str(step.get("task_id") or "") == task_id
                ),
                {},
            )
            unmet = [
                str(dep)
                for dep in target.get("depends_on") or []
                if statuses.get(str(dep)) not in {"succeeded", "skipped"}
            ]
            if unmet:
                from agent.runtime.scheduler import canonical_args_hash

                return GovernanceDecision(
                    allowed=False,
                    code="dependency_not_ready",
                    message=f"任务依赖尚未成功：{','.join(unmet)}",
                    args_hash=canonical_args_hash(dict(args or {})),
                )
        decision = self._governance.authorize(
            session=session,
            tool_id=tool_id,
            args=dict(args or {}),
            controller=controller,
            task_id=task_id,
            confirmed_scopes=confirmed_scopes,
        )
        session.agent_run_state = controller.snapshot()
        if decision.allowed and decision.approval_scope not in {"", "allow_live"}:
            # Persist one-shot approval consumption before entering the tool
            # handler so a process restart cannot replay the same grant.
            self.store.persist(session)
        return decision

    def _governance_denied_events(
        self,
        session: AgentSession,
        *,
        tool_id: str,
        decision: Any,
    ) -> Iterator[dict[str, Any]]:
        yield self._emit(
            session,
            {
                "type": "governance_denied",
                "tool": tool_id,
                "code": decision.code,
                "detail": decision.message,
                "args_hash": decision.args_hash,
                "approval_scope": decision.approval_scope or None,
            },
        )
        yield self._emit(
            session,
            {
                "type": "tool_end",
                "tool": tool_id,
                "ok": False,
                "status": "denied",
                "error_code": decision.code,
                "error": decision.message,
                "args_hash": decision.args_hash,
            },
        )

    def _emit_live(self, session: AgentSession, event: dict[str, Any]) -> dict[str, Any]:
        """Emit a live-only event (token deltas) without durable persistence.

        ``assistant_delta`` must not hit Postgres / transcript / claim checks —
        only the final ``assistant`` event is durable.
        """
        kind = str(event.get("type") or "")
        controller = self._run_controller(session)
        if controller.interruption_requested and kind not in {
            "tool_end",
            "run_interrupted",
            "done",
            "error",
        }:
            raise RunInterrupted(controller.interrupt_reason or "user_guidance")
        event.setdefault("run_id", controller.run_id)
        event.setdefault("turn_id", controller.run_id)
        event["live_only"] = True
        active = session.active_run
        if isinstance(active, dict) and str(active.get("run_id") or "") == controller.run_id:
            active["heartbeat_at"] = utc_now()
        return event

    def _emit(self, session: AgentSession, event: dict[str, Any]) -> dict[str, Any]:
        kind = str(event.get("type") or "")
        controller = self._run_controller(session)
        if controller.interruption_requested and kind not in {
            "tool_end",
            "run_interrupted",
            "done",
            "error",
        }:
            raise RunInterrupted(controller.interrupt_reason or "user_guidance")
        event.setdefault("run_id", controller.run_id)
        event.setdefault("turn_id", controller.run_id)
        if kind == "agent_plan":
            raw_steps = event.get("tasks") or event.get("steps") or []
            within_budget, reason = controller.register_plan(len(raw_steps))
            event.setdefault("run_id", controller.run_id)
            event["budget"] = controller.budget.to_dict()
            if not within_budget:
                event.setdefault("diagnostics", []).append(reason)
        elif kind == "tool_start":
            call = controller.active_call(str(event.get("tool") or ""))
            event.setdefault("run_id", controller.run_id)
            event.setdefault("governed", call is not None)
            if call is not None:
                event.setdefault("call_id", call.call_id)
                event.setdefault("task_id", call.task_id)
                event.setdefault("args_hash", call.args_hash)
                event.setdefault("timeout_sec", call.timeout_sec)
                event.setdefault("writes_selection", call.writes_selection)
            args_hash = str(event.get("args_hash") or canonical_args_hash(dict(event.get("args") or {})))
            event["args_hash"] = args_hash
            checkpoint = {
                "checkpoint_id": f"cp-{uuid.uuid4().hex[:12]}",
                "checkpoint_key": hashlib.sha256(
                    f"{event.get('tool') or ''}:{args_hash}".encode("utf-8")
                ).hexdigest(),
                "run_id": controller.run_id,
                "retry_of_run_id": str((session.active_run or {}).get("retry_of_run_id") or ""),
                "call_id": str(event.get("call_id") or ""),
                "task_id": str(event.get("task_id") or ""),
                "tool": str(event.get("tool") or ""),
                "args": copy.deepcopy(event.get("args") or {}),
                "args_hash": args_hash,
                "status": "running",
                "attempt": 1 + sum(
                    1
                    for item in session.tool_checkpoints
                    if item.get("tool") == event.get("tool") and item.get("args_hash") == args_hash
                ),
                "started_at": utc_now(),
                "retryable": True,
                "reused_from_checkpoint_id": str(event.get("reused_from_checkpoint_id") or ""),
            }
            event["checkpoint_id"] = checkpoint["checkpoint_id"]
            event["checkpoint_key"] = checkpoint["checkpoint_key"]
            session.tool_checkpoints.append(checkpoint)
            session.tool_checkpoints = session.tool_checkpoints[-100:]
        elif kind == "tool_end":
            tool_id = str(event.get("tool") or "")
            call = controller.active_call(tool_id)
            observation = normalize_tool_end(
                event,
                call=call,
                observation_limit=controller.budget.max_observation_chars,
            )
            event["observation"] = observation.to_dict()
            event.setdefault("run_id", controller.run_id)
            event.setdefault("call_id", call.call_id if call else "")
            event.setdefault("task_id", observation.task_id)
            event.setdefault("args_hash", observation.args_hash)
            controller.finish_call(
                tool_id,
                status=observation.status,
                observation_signature=observation.signature,
            )
            session.working_memory.append(
                {
                    "turn_id": controller.run_id,
                    "iteration": 0,
                    "objective": str(
                        (session.active_plan or {}).get("goal")
                        if isinstance(session.active_plan, dict)
                        else ""
                    )[:700],
                    "tasks": [],
                    "tool_calls": [
                        {
                            "tool": tool_id,
                            "status": (
                                "succeeded"
                                if observation.ok
                                else "failed"
                            ),
                            "args_hash": observation.args_hash,
                            "observation": observation.to_dict(),
                        }
                    ],
                    "decision": "observed",
                    "reason": observation.status,
                    "recorded_at_unix": int(time.time()),
                }
            )
            session.working_memory = session.working_memory[-24:]
            session.agent_run_state = controller.snapshot()
            checkpoint = next(
                (
                    item
                    for item in reversed(session.tool_checkpoints)
                    if item.get("run_id") == controller.run_id
                    and item.get("tool") == tool_id
                    and item.get("status") == "running"
                ),
                None,
            )
            if checkpoint is not None:
                checkpoint["status"] = "succeeded" if observation.ok else observation.status
                checkpoint["ended_at"] = utc_now()
                checkpoint["retryable"] = not observation.ok
                checkpoint["observation"] = observation.to_dict()
                checkpoint["terminal_event"] = {
                    key: copy.deepcopy(event.get(key))
                    for key in (
                        "ok", "status", "digest", "error", "error_code", "summary",
                        "source", "job_id", "participates_in_ranking", "ranking_changed",
                        "writes_selection",
                    )
                    if key in event
                }
                event["checkpoint_id"] = checkpoint.get("checkpoint_id")
        elif kind == "done":
            done_status = str(event.get("status") or "succeeded").strip().lower()
            if controller.status == "running":
                if done_status in {"failed", "interrupted", "denied"}:
                    controller.status = done_status
                else:
                    controller.status = "completed"
            else:
                controller.complete()
            session.agent_run_state = controller.snapshot()
            event.setdefault("run", session.agent_run_state)
            active = session.active_run if isinstance(session.active_run, dict) else {}
            if active and str(active.get("run_id") or "") == controller.run_id:
                mapped = {
                    "completed": "succeeded",
                    "succeeded": "succeeded",
                    "failed": "failed",
                    "interrupted": "interrupted",
                    "denied": "denied",
                }.get(done_status or controller.status, "succeeded")
                if controller.status == "completed":
                    mapped = "succeeded"
                elif controller.status in {"failed", "interrupted"}:
                    mapped = controller.status
                active["status"] = mapped
                active["ended_at"] = utc_now()
                active["heartbeat_at"] = utc_now()
                session.active_run = active

        self._observe_plan_event(session, event)
        if event.get("type") == "assistant" and event.get("text"):
            original = str(event["text"])
            violations = verify_assistant_claims(session, str(event["text"]))
            if violations:
                event["text"] = evidence_correction(session, violations)
                event["claim_verification"] = {
                    "ok": False,
                    "violations": [v.code for v in violations],
                }
            rendered = str(event["text"])
            capture = _BRANCH_ASSISTANT_CAPTURE.get()
            if capture is not None:
                # A compound turn merges branch answers after all observations
                # are available. Do not leak a competing partial answer into
                # the transcript or event log.
                capture.append(rendered)
                return {
                    "type": "branch_observation",
                    "kind": "assistant",
                    "text": rendered,
                    "claim_ceiling": claim_ceiling_default(),
                }
            # Some successful tool paths emit their completion directly. Keep
            # the durable transcript complete even when the caller did not
            # manually append the same assistant message first.
            if (
                session.messages
                and session.messages[-1].get("role") == "assistant"
                and session.messages[-1].get("text") == original
            ):
                session.messages[-1]["text"] = rendered
            elif not (
                session.messages
                and session.messages[-1].get("role") == "assistant"
                and session.messages[-1].get("text") == rendered
            ):
                session.messages.append({"role": "assistant", "text": rendered})
        event.setdefault("claim_ceiling", claim_ceiling_default())
        emitted = self.store.append_event(session, event)
        active = session.active_run
        if isinstance(active, dict) and str(active.get("run_id") or "") == controller.run_id:
            active["last_event_seq"] = int(emitted.get("seq") or session.event_seq)
            active["heartbeat_at"] = utc_now()
            if kind == "done":
                controller_status = str(controller.status or "")
                requested_status = str(event.get("status") or "")
                if requested_status not in {"succeeded", "failed", "cancelled", "interrupted"}:
                    # Bare done: prefer explicit event status; otherwise map
                    # controller state. partial/running with a completed turn
                    # still counts as succeeded unless stop_reason says otherwise.
                    stop_reason = str(getattr(controller, "stop_reason", "") or "")
                    if controller_status == "completed":
                        requested_status = "succeeded"
                    elif controller_status in {"failed", "cancelled", "interrupted"}:
                        requested_status = controller_status
                    elif stop_reason:
                        requested_status = "failed"
                    else:
                        requested_status = "succeeded"
                    event["status"] = requested_status
                active["status"] = requested_status
                active["ended_at"] = utc_now()
                archived = copy.deepcopy(active)
                if not any(
                    item.get("run_id") == archived.get("run_id")
                    for item in session.agent_run_history
                ):
                    session.agent_run_history.append(archived)
                    session.agent_run_history = session.agent_run_history[-30:]
            session.active_run = active
            self.store.persist(session)
        if kind == "done":
            with self._run_controllers_guard:
                self._run_controllers.pop(session.session_id, None)
        return emitted

    @staticmethod
    def _observe_plan_event(session: AgentSession, event: dict[str, Any]) -> None:
        """Project existing tool events onto durable plan-step observations."""
        kind = str(event.get("type") or "")
        if kind == "agent_plan":
            plan_id = uuid.uuid4().hex[:12]
            event["plan_id"] = plan_id
            raw_steps = event.get("tasks") or event.get("steps") or []
            graph = TaskGraph.from_steps(
                goal=str(event.get("goal") or ""),
                steps=raw_steps,
                sequential_default=True,
            )
            session.active_plan = {
                "plan_id": plan_id,
                "run_id": str(event.get("run_id") or ""),
                "goal": str(event.get("goal") or ""),
                "action": str(event.get("action") or ""),
                "expected_artifacts": list(event.get("expected_artifacts") or []),
                "diagnostics": list(event.get("diagnostics") or []),
                "status": "running",
                "steps": [task.to_dict() for task in graph.tasks],
            }
            return

        active = session.active_plan
        if not isinstance(active, dict):
            return
        tool = str(event.get("tool") or "")
        if kind == "tool_start" and tool:
            for step in active.get("steps") or []:
                task_match = (
                    not event.get("task_id")
                    or step.get("task_id") == event.get("task_id")
                )
                if (
                    task_match
                    and step.get("tool") == tool
                    and step.get("status") == "pending"
                ):
                    step["status"] = "running"
                    step["observation"] = {
                        "started": True,
                        "args": event.get("args") or {},
                        "args_hash": event.get("args_hash") or "",
                        "call_id": event.get("call_id") or "",
                    }
                    break
            return
        if kind == "tool_end" and tool:
            for step in active.get("steps") or []:
                task_match = (
                    not event.get("task_id")
                    or step.get("task_id") == event.get("task_id")
                )
                if (
                    task_match
                    and step.get("tool") == tool
                    and step.get("status") in {"pending", "running"}
                ):
                    ok = bool(event.get("ok"))
                    observation = dict(event.get("observation") or {})
                    observation_status = str(observation.get("status") or "")
                    step["status"] = (
                        "succeeded"
                        if ok
                        else ("denied" if observation_status == "denied" else "failed")
                    )
                    step["observation"] = observation or {
                        "ok": ok,
                        "digest": event.get("digest") or {},
                        "error": event.get("error") or "",
                    }
                    if not ok:
                        active["status"] = "failed"
                    break
            return
        if kind == "task_start" and event.get("task_id"):
            for step in active.get("steps") or []:
                if (
                    step.get("task_id") == event.get("task_id")
                    and step.get("status") == "pending"
                ):
                    step["status"] = "running"
                    step["observation"] = {"started": True}
                    break
            return
        if kind == "task_end" and event.get("task_id"):
            for step in active.get("steps") or []:
                if step.get("task_id") != event.get("task_id"):
                    continue
                status = str(event.get("status") or "succeeded")
                step["status"] = status
                step["observation"] = dict(event.get("observation") or {})
                if status not in {"succeeded", "skipped"}:
                    active["status"] = "failed"
                break
            return
        if kind == "done":
            if active.get("status") != "failed":
                pending_steps = [
                    step
                    for step in active.get("steps") or []
                    if step.get("status") == "pending"
                ]
                if pending_steps:
                    # A compiled plan is an audit promise. If the stream ends
                    # before a step has even started, show it as incomplete
                    # rather than silently converting it to a success.
                    for step in pending_steps:
                        step["status"] = "not_executed"
                        step["observation"] = {"reason": "stream_ended_before_execution"}
                    active["status"] = "incomplete"
                else:
                    active["status"] = "completed"
            session.plan_history.append(copy.deepcopy(active))
            session.plan_history = session.plan_history[-20:]
            session.active_plan = None

    def handle_message(
        self,
        session: AgentSession,
        text: str,
        *,
        run_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        controller = self._begin_agent_turn(session, run_id=run_id)
        with cancel_scope(controller.cancel_event):
            yield from self._handle_message_body(session, text)

    def _handle_message_body(
        self,
        session: AgentSession,
        text: str,
    ) -> Iterator[dict[str, Any]]:
        lo, hi = self.registry.resolve_top_n_bounds()

        # Pending Top-N over-limit confirmation (same session only).
        pending = session.pending_top_confirm
        if isinstance(pending, dict) and pending:
            hi = int(pending.get("top_n_max") or pending.get("top_n") or hi)
            lo = int(pending.get("top_n_min") or lo)
            decision, why = self._classify_top_confirm(text, pending)
            yield self._emit(
                session,
                {
                    "type": "thinking",
                    "text": f"判断用户对 Top 上限确认的态度：{decision}（{why}）。",
                },
            )
            if decision == "affirm":
                session.pending_top_confirm = None
                capped = int(pending.get("top_n") or hi)
                session.top_n = capped
                intent = AgentIntent(
                    want_csv=bool(pending.get("want_csv")),
                    want_pdf=bool(pending.get("want_pdf")),
                    want_reserve=bool(pending.get("want_reserve")),
                    want_bundle=bool(pending.get("want_bundle")),
                    top_n=capped,
                    raw_text=text,
                    reason=f"用户确认按上限 Top{capped} 输出",
                    skill_ids=tuple(pending.get("skill_ids") or ()),
                    wants_tools=True,
                    mentions=(),
                    mention_action="",
                    requested_top_n=int(pending.get("requested_top_n") or capped),
                    top_n_over_limit=False,
                    top_n_max=hi,
                    top_n_min=lo,
                )
                yield from self._handle_intent(session, text, intent)
                return
            if decision == "negate":
                session.pending_top_confirm = None
                turn_attachments: list[dict[str, str]] = []
                if session.sdf_ui_pending and session.sdf_filename and session.sdf_bytes:
                    turn_attachments.append(
                        {"kind": "sdf", "filename": session.sdf_filename}
                    )
                    session.sdf_ui_pending = False
                session.messages.append(
                    {
                        "role": "user",
                        "text": text,
                        "attachments": turn_attachments,
                        "created_at": utc_now(),
                        "run_id": str((session.active_run or {}).get("run_id") or ""),
                        "turn_id": str((session.active_run or {}).get("turn_id") or ""),
                    }
                )
                reply = (
                    f"好的，已取消。相关技能/工具当前上限是 Top{hi}。"
                    f"若要导出，直接说例如「生成 top{hi} 候选清单 csv」即可。"
                )
                session.messages.append({"role": "assistant", "text": reply})
                yield self._emit(session, {"type": "assistant", "text": reply})
                yield self._emit(session, {"type": "done"})
                self.store.persist(session)
                return
            # other → drop stale confirm and continue as a fresh request
            session.pending_top_confirm = None

        # Resume an incomplete executable request before classifying this
        # short reply as standalone chat. This turns “导出 CSV → 需要 → 10”
        # into one continuous operation and keeps the slots durable across a
        # process restart.
        pending_action = session.pending_action
        if isinstance(pending_action, dict) and pending_action:
            compact = (text or "").strip()
            if _PENDING_CANCEL_RE.fullmatch(compact):
                session.pending_action = None
                self._prepare_turn(session, text)
                reply = "好的，已取消尚未启动的筛选/导出请求。"
                session.messages.append({"role": "assistant", "text": reply})
                yield self._emit(session, {"type": "assistant", "text": reply})
                yield self._emit(session, {"type": "done"})
                self.store.persist(session)
                return

            number_match = _PENDING_TOP_N_REPLY_RE.fullmatch(compact)
            if number_match:
                requested = int(number_match.group(1))
                pending_action["top_n"] = min(max(requested, lo), hi)
                pending_action["requested_top_n"] = requested

            missing_sdf = not bool(session.sdf_bytes)
            missing_top_n = pending_action.get("top_n") is None
            if number_match:
                continuation_act, continuation_why = "continue", "structural_top_n_reply"
            else:
                continuation_act, continuation_why = self._classify_pending_continuation(
                    session, compact, pending_action
                )
            is_continuation = continuation_act in {"continue", "status"}
            if is_continuation and (missing_sdf or missing_top_n):
                self._prepare_turn(session, text)
                if continuation_act == "status":
                    prefix = "尚未启动工具调用；"
                else:
                    prefix = "已续接你之前的导出请求；"
                if missing_sdf:
                    reply = prefix + "目前还缺少 SDF 化合物库，请先上传 .sdf 附件。"
                else:
                    reply = prefix + "附件已就绪，还需要你告诉我候选数量，例如回复「10」。"
                pending_action["missing_slots"] = [
                    slot
                    for slot, missing in (("sdf", missing_sdf), ("top_n", missing_top_n))
                    if missing
                ]
                session.messages.append({"role": "assistant", "text": reply})
                yield self._emit(
                    session,
                    {
                        "type": "thinking",
                        "text": f"待办续接：{continuation_act}（{continuation_why}）",
                    },
                )
                yield self._emit(session, {"type": "assistant", "text": reply})
                yield self._emit(session, {"type": "done"})
                self.store.persist(session)
                return

            if is_continuation and not missing_sdf and not missing_top_n:
                requested = int(pending_action.get("requested_top_n") or pending_action["top_n"])
                capped = int(pending_action["top_n"])
                intent = AgentIntent(
                    want_csv=bool(pending_action.get("want_csv")),
                    want_pdf=bool(pending_action.get("want_pdf")),
                    want_reserve=bool(pending_action.get("want_reserve")),
                    want_bundle=bool(pending_action.get("want_bundle")),
                    top_n=capped,
                    raw_text=text,
                    reason=f"续接多轮请求并补齐 Top{requested}",
                    skill_ids=tuple(pending_action.get("skill_ids") or ("masld_nominate",)),
                    wants_tools=True,
                    requested_top_n=requested,
                    top_n_over_limit=requested > hi,
                    top_n_max=hi,
                    top_n_min=lo,
                )
                session.pending_action = None
                session.pending_goal = None
                if not intent.top_n_over_limit:
                    resumed_steps: list[dict[str, Any]] = [
                        {"tool": "score_and_rank", "args": {"top_n": capped}}
                    ]
                    expected_artifacts: list[str] = []
                    if intent.want_csv:
                        resumed_steps.append(
                            {"tool": "export_nomination", "args": {"tier": "primary"}}
                        )
                        expected_artifacts.append("nomination_csv")
                    if intent.want_reserve:
                        resumed_steps.append(
                            {"tool": "export_nomination", "args": {"tier": "reserve"}}
                        )
                        expected_artifacts.append("reserve_csv")
                    if intent.want_pdf:
                        resumed_steps.append(
                            {"tool": "start_mechanism_report", "args": {}}
                        )
                        expected_artifacts.append("mechanism_pdf")
                    if intent.want_bundle:
                        resumed_steps.append(
                            {"tool": "export_submission_bundle", "args": {}}
                        )
                        expected_artifacts.append("submission_bundle")
                    yield self._emit(
                        session,
                        {
                            "type": "agent_plan",
                            "goal": str(pending_action.get("source_text") or text),
                            "action": "execute",
                            "steps": resumed_steps,
                            "expected_artifacts": expected_artifacts,
                            "diagnostics": ["resumed_from_pending_action"],
                        },
                    )
                yield from self._handle_intent(session, text, intent)
                return

            # A complete structural deliverable supersedes the unfinished request.
            # Do not use online keyword tables (_DIRECT_DELIVERABLE_RE); parse_intent
            # product slots are the source of truth. Ranking follow-up candidates
            # (wants_tools without csv/pdf/bundle) must not clear pending.
            probe = parse_intent(
                compact,
                default_top_n=session.top_n,
                top_n_min=lo,
                top_n_max=hi,
            )
            if probe.wants_tools and (
                probe.want_csv
                or probe.want_pdf
                or probe.want_reserve
                or probe.want_bundle
                or probe.force_rescreen
            ):
                session.pending_action = None

        # Planner clarify for missing SDF historically only set pending_goal (chat),
        # so short follow-ups like「提供了」never resumed. When the library is now
        # bound, treat continuation the same as pending_action resume.
        pending_goal = session.pending_goal
        if (
            isinstance(pending_goal, dict)
            and pending_goal
            and bool(session.sdf_bytes)
            and not (
                isinstance(session.pending_action, dict) and session.pending_action
            )
        ):
            source = str(pending_goal.get("source_text") or "").strip()
            compact_goal = (text or "").strip()
            if source and not _PENDING_CANCEL_RE.fullmatch(compact_goal):
                probe_goal = parse_intent(
                    compact_goal,
                    default_top_n=session.top_n,
                    top_n_min=lo,
                    top_n_max=hi,
                )
                new_deliverable = probe_goal.wants_tools and (
                    probe_goal.want_csv
                    or probe_goal.want_pdf
                    or probe_goal.want_reserve
                    or probe_goal.want_bundle
                    or probe_goal.force_rescreen
                )
                if not new_deliverable:
                    synthetic = {
                        "source_text": source,
                        "top_n": session.top_n,
                        "missing_slots": [],
                    }
                    goal_act, goal_why = self._classify_pending_continuation(
                        session, compact_goal, synthetic
                    )
                    if goal_act in {"continue", "status"}:
                        source_intent = parse_intent(
                            source,
                            default_top_n=session.top_n,
                            top_n_min=lo,
                            top_n_max=hi,
                        )
                        if source_intent.wants_tools:
                            yield self._emit(
                                session,
                                {
                                    "type": "thinking",
                                    "text": (
                                        f"待确认目标续接：{goal_act}（{goal_why}）；"
                                        f"会话已绑定 SDF「{session.sdf_filename or 'library.sdf'}」。"
                                    ),
                                },
                            )
                            if goal_act == "status":
                                self._prepare_turn(session, text)
                                reply = (
                                    f"化合物库「{session.sdf_filename or 'library.sdf'}」"
                                    "已绑定，此前的筛选/导出请求尚未启动。"
                                    "回复「继续」即可按原目标执行。"
                                )
                                session.messages.append(
                                    {"role": "assistant", "text": reply}
                                )
                                yield self._emit(
                                    session, {"type": "assistant", "text": reply}
                                )
                                yield self._emit(session, {"type": "done"})
                                self.store.persist(session)
                                return
                            session.pending_goal = None
                            resumed = AgentIntent(
                                want_csv=bool(source_intent.want_csv)
                                or bool(source_intent.execution_requested),
                                want_pdf=bool(source_intent.want_pdf),
                                want_reserve=bool(source_intent.want_reserve),
                                want_bundle=bool(source_intent.want_bundle),
                                top_n=int(source_intent.top_n),
                                raw_text=text,
                                reason=(
                                    f"续接待确认目标并已绑定 SDF："
                                    f"{pending_goal.get('goal') or source}"
                                ),
                                skill_ids=tuple(
                                    source_intent.skill_ids or ("masld_nominate",)
                                ),
                                wants_tools=True,
                                requested_top_n=source_intent.requested_top_n,
                                top_n_over_limit=bool(source_intent.top_n_over_limit),
                                top_n_max=hi,
                                top_n_min=lo,
                                force_rescreen=bool(source_intent.force_rescreen),
                                execution_requested=True,
                            )
                            yield self._emit(
                                session,
                                {
                                    "type": "agent_plan",
                                    "goal": str(pending_goal.get("goal") or source),
                                    "action": "execute",
                                    "steps": [
                                        {
                                            "tool": "score_and_rank",
                                            "args": {"top_n": resumed.top_n},
                                        }
                                    ],
                                    "expected_artifacts": (
                                        ["nomination_csv"] if resumed.want_csv else []
                                    ),
                                    "diagnostics": ["resumed_from_pending_goal"],
                                },
                            )
                            yield from self._handle_intent(session, text, resumed)
                            return

        # First pass without skill-specific bounds; refine after intent known.
        intent = parse_intent(
            text,
            default_top_n=session.top_n,
            top_n_min=lo,
            top_n_max=hi,
        )
        if intent.wants_tools and intent.skill_ids:
            lo2, hi2 = self.registry.resolve_top_n_bounds(skill_ids=intent.skill_ids)
            if (lo2, hi2) != (lo, hi):
                lo, hi = lo2, hi2
                intent = parse_intent(
                    text,
                    default_top_n=session.top_n,
                    top_n_min=lo,
                    top_n_max=hi,
                )
        # Explicit rescreen without a new TopN must not inherit a stale session
        # preference left by an aborted Top100→Top50 confirm.
        if intent.force_rescreen and intent.requested_top_n is None:
            default_n = self._profile_default_top_n(session)
            session.top_n = default_n
            session.pending_top_confirm = None
            session.pending_goal = None
            session.pending_action = None
            intent = parse_intent(
                text,
                default_top_n=default_n,
                top_n_min=lo,
                top_n_max=hi,
            )
        # Tool-shaped surface text is still ambiguous: let the conversation
        # model classify the dialog act before executing anything.  The
        # structural parser only supplies candidate parameters and a safe
        # offline fallback; it is not the source of truth for follow-ups.
        # ranking_question_fallback / direct_deliverable regexes are LLM-down
        # only (see _classify_request_action / _classify_execution_gate defaults).
        session.turn_execution_gate = None
        session.turn_execution_dialog_act = None
        if intent.wants_tools and not intent.mentions and not intent.query_evidence:
            ranking_molecule_id = None
            gate, gate_meta = self._classify_execution_gate(session, text, intent)
            session.turn_execution_gate = gate
            session.turn_execution_dialog_act = str(
                gate_meta.get("dialog_act") or ""
            )
            yield self._emit(
                session,
                {
                    "type": "thinking",
                    "text": (
                        f"执行门控：{gate}"
                        f"/{gate_meta.get('dialog_act', '')}"
                        f"（{gate_meta.get('reason', '')}）"
                    ),
                },
            )
            if gate == "block":
                dialog_act = str(gate_meta.get("dialog_act") or "discuss_only")
                if dialog_act in {"cancel_pending", "discuss_only", "defer_execute"}:
                    session.pending_goal = None
                    session.pending_action = None
                    session.pending_top_confirm = None
                    action, why = (
                        "chat",
                        f"execution_gate_block:{dialog_act}:{gate_meta.get('reason', '')}",
                    )
                elif ranking_question_fallback(text)[0]:
                    # Offline/clarify gate must not swallow ranking follow-ups;
                    # request-action (LLM or structural fallback) owns explain.
                    action, why = self._classify_request_action(session, text, intent)
                else:
                    action, why = (
                        "chat",
                        f"execution_gate_block:{dialog_act}:{gate_meta.get('reason', '')}",
                    )
            else:
                if gate_meta.get("force_rescreen") or intent.force_rescreen:
                    intent = replace(intent, force_rescreen=True)
                    session.pending_goal = None
                    session.pending_action = None
                    session.pending_top_confirm = None
                    if intent.requested_top_n is None:
                        default_n = self._profile_default_top_n(session)
                        session.top_n = default_n
                        if intent.top_n != default_n:
                            old_n = int(intent.top_n)
                            intent = replace(
                                intent,
                                top_n=default_n,
                                reason=(
                                    str(intent.reason or "").replace(
                                        f"Top{old_n}", f"Top{default_n}"
                                    )
                                    or f"需要：Top{default_n} 候选 CSV"
                                ),
                            )
                action, why = self._classify_request_action(session, text, intent)
            if action == "explain_ranking":
                _, ranking_molecule_id = ranking_question_fallback(text)
            if action != "execute_tools":
                intent = replace(
                    intent,
                    want_csv=False,
                    want_pdf=False,
                    skill_ids=(),
                    wants_tools=False,
                    want_reserve=False,
                    want_bundle=False,
                    execution_requested=False,
                    force_rescreen=False,
                    reason=(
                        "询问上一轮候选排名原因，不重新筛选或导出"
                        if action == "explain_ranking"
                        else "一般对话，暂不调用筛选工具"
                    ),
                    explain_ranking=action == "explain_ranking",
                    ranking_molecule_id=ranking_molecule_id,
                    ranking_positions=(
                        extract_ranking_positions(text)
                        if action == "explain_ranking"
                        else ()
                    ),
                    ranking_position_subject=(
                        action == "explain_ranking"
                        and ranking_position_subject_fallback(text)
                    ),
                )
            yield self._emit(
                session,
                {
                    "type": "thinking",
                    "text": (
                        "我已确认你的目标，接下来会为你准备所需结果。"
                        if action == "execute_tools"
                        else "我先理解你的问题，再决定是否需要执行筛选。"
                    ),
                },
            )
        # Mentions: refine introduce vs invoke with LLM (parse_intent only sets safe default).
        if intent.mentions:
            if any(m.id in _EVIDENCE_MENTION_IDS for m in intent.mentions):
                # R0 证据查询 mention 是显式工具选择；离线时也应执行或提示缺参，
                # 不依赖可选 LLM 才能从“介绍”切到“试用”。
                action, why = "invoke", "explicit_read_only_evidence_mention"
            else:
                action, why = self._classify_mention_action(text, intent.mentions)
            intent = replace(
                intent,
                mention_action=action,
                reason=(
                    f"点选 {', '.join(m.raw for m in intent.mentions)}，"
                    + ("试用调用" if action == "invoke" else "介绍说明")
                    + f"（{why}）"
                ),
            )
        if intent.mentions and intent.companion_text:
            yield from self._handle_compound_turn(session, text, intent)
            return
        # Emit the registry-compiled plan before dispatch.  The legacy intent
        # adapter still supplies candidate skills during this migration, while
        # the execution contract—not keyword branches—defines their ordered
        # tool dependencies and preconditions.
        if intent.skill_ids and not intent.mentions:
            executable_tools = {
                tool_id
                for tool_id in self.registry.tools
                if callable(getattr(self, f"_execute_{tool_id}", None))
            }
            plan, diagnostics = plan_for_skills(
                goal=intent.reason,
                action="execute" if intent.wants_tools else "chat",
                skill_ids=intent.skill_ids,
                skills=self.registry.skills,
                tools=self.registry.tools,
                capabilities=session_capabilities(session),
                executable_tools=executable_tools,
            )
            planned_steps: list[PlanStep] = []
            for step in plan.steps:
                if step.tool_id == "score_and_rank":
                    planned_steps.append(
                        PlanStep(tool_id=step.tool_id, args={"top_n": intent.top_n})
                    )
                elif step.tool_id == "export_nomination":
                    if intent.want_csv:
                        planned_steps.append(
                            PlanStep(tool_id=step.tool_id, args={"tier": "primary"})
                        )
                    if intent.want_reserve:
                        planned_steps.append(
                            PlanStep(tool_id=step.tool_id, args={"tier": "reserve"})
                        )
                else:
                    planned_steps.append(step)
            plan_steps = tuple(planned_steps)
            yield self._emit(
                session,
                {
                    "type": "agent_plan",
                    "goal": plan.goal,
                    "action": plan.action,
                    "steps": [
                        {"tool": step.tool_id, "args": step.args}
                        for step in plan_steps
                    ],
                    "expected_artifacts": list(plan.expected_artifacts),
                    "diagnostics": diagnostics,
                },
            )
        yield from self._handle_intent(session, text, intent)

    def _profile_default_top_n(self, session: AgentSession) -> int:
        """Profile / session factory default TopN (not the sticky session preference)."""
        raw = getattr(session, "profile_default_top_n", None)
        if raw is not None:
            try:
                value = int(raw)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass
        return int(_PROFILE_DEFAULT_TOP_N)

    def _classify_execution_gate(
        self, session: AgentSession, text: str, intent: AgentIntent
    ) -> tuple[str, dict[str, Any]]:
        """LLM gate: whether this turn may call tools.

        Returns ``(allow|block, meta)`` where meta includes dialog_act,
        force_rescreen, and reason. Prompt describes semantic principles only—
        not a phrase whitelist for stop/continue/skip.
        """
        history_lines: list[str] = []
        for message in session.messages[-6:]:
            role = str(message.get("role") or "")
            body = str(message.get("text") or "").strip()
            if role in {"user", "assistant"} and body:
                history_lines.append(f"{role}: {body[:500]}")
        history = "\n".join(history_lines) if history_lines else "（无）"
        # Offline bias only: when LLM is down, structural deliverable / discuss
        # cues seed the default. Online LLM results always win over these regexes.
        direct = _is_direct_deliverable_request(intent, text)
        prefer_discuss = _offline_prefer_discuss(text)
        if direct and not prefer_discuss:
            offline_act, offline_gate = "execute_now", "allow"
        elif prefer_discuss:
            offline_act, offline_gate = "discuss_only", "block"
        else:
            offline_act, offline_gate = "clarify", "block"
        data, status = llm_json_object(
            system=(
                "你是 MolMind 的执行门控分类器。只返回 JSON："
                '{"dialog_act":"execute_now|defer_execute|discuss_only|cancel_pending|clarify",'
                '"execution_gate":"allow|block","force_rescreen":true|false,"reason":"..."}。'
                "任务：判断用户对本轮是否调用工具/推进筛选或导出流水线的意图。"
                "按语义原则泛化，不要把任何表面用词当成完整规则表或口令白名单；"
                "同一意图的不同说法应得到同一 dialog_act。"
                "execution_gate=allow 仅当 dialog_act=execute_now；其余均为 block。"
                "execute_now：用户要本轮产出结果或推进已声明的交付物（筛选、导出、报告、打包、重跑等）。"
                "discuss_only：只讨论条件、策略、利弊或配置，本轮不要调用工具。"
                "defer_execute：明确暂缓本轮执行，稍后再跑。"
                "cancel_pending：取消排队、进行中或待确认的执行。"
                "clarify：信息不足，需先问清再决定是否执行。"
                "force_rescreen=true 仅当用户要丢弃旧条件/旧冻结并重新开跑筛选。"
                "即使原文出现筛选、Top、CSV、候选等词，若整体是讨论、暂缓、跳过或取消，仍应 block。"
            ),
            user=(
                f"已有筛选结果：{'有' if session.last_result is not None else '无'}；"
                f"结构解析候选：{intent.reason}；"
                f"待确认目标：{'有' if isinstance(session.pending_goal, dict) else '无'}；"
                f"待确认动作：{'有' if isinstance(session.pending_action, dict) else '无'}。\n"
                f"最近对话：\n{history}\n"
                f"用户本轮原文：{text}\n"
                "请判定 dialog_act、execution_gate 与 force_rescreen。"
            ),
            default={
                "dialog_act": offline_act,
                "execution_gate": offline_gate,
                "force_rescreen": bool(getattr(intent, "force_rescreen", False)),
                "reason": "offline_structural_fallback",
            },
            purpose="agent_chat",
            max_tokens=200,
            timeout_sec=8.0,
        )
        acts = {
            "execute_now",
            "defer_execute",
            "discuss_only",
            "cancel_pending",
            "clarify",
        }
        dialog_act = str(data.get("dialog_act") or offline_act).strip().lower()
        if dialog_act not in acts:
            dialog_act = offline_act
        gate = str(data.get("execution_gate") or "").strip().lower()
        if gate not in {"allow", "block"}:
            gate = "allow" if dialog_act == "execute_now" else "block"
        if dialog_act != "execute_now":
            gate = "block"
        else:
            gate = "allow"
        force_rescreen = bool(data.get("force_rescreen")) or bool(
            getattr(intent, "force_rescreen", False)
        )
        if gate != "allow":
            force_rescreen = False
        reason = str(data.get("reason") or status or "llm").strip() or "llm"
        if status != "ok":
            reason = f"{status};{reason}"
        return gate, {
            "dialog_act": dialog_act,
            "force_rescreen": force_rescreen,
            "reason": reason,
            "status": status,
        }

    def _classify_mention_action(
        self, text: str, mentions: tuple[MentionRef, ...]
    ) -> tuple[str, str]:
        """Return (introduce|invoke, reason). Prefer LLM; else safe introduce."""
        listed = ", ".join(f"{m.kind}:{m.id}" for m in mentions)
        decision, why = llm_json_decision(
            system=(
                "你在判断用户对已点选插件/技能/工具的意图。"
                "只返回 JSON：{\"decision\":\"introduce|invoke\",\"reason\":\"...\"}。"
                "introduce=介绍/说明它是什么；invoke=现在就试用/调用该工具或技能。"
                "不确定时选 introduce，不要擅自执行。"
            ),
            user=f"点选对象：{listed}\n用户原文：{text}\n请判定 decision。",
            allowed={"introduce", "invoke"},
            default="introduce",
        )
        return decision, why

    def _scp_skill_catalog_meta(self, skill_id: str) -> dict[str, str]:
        sid = str(skill_id or "").strip()
        title = sid or "科研 Skill"
        description = ""
        if not sid:
            return {"skill_id": "", "title": title, "description": description}
        try:
            item = self.scp.catalog.get(sid)
        except Exception:
            item = None
        if isinstance(item, dict):
            title = str(item.get("title") or title).strip() or title
            description = str(item.get("description") or "").strip()
        return {"skill_id": sid, "title": title, "description": description}

    def _install_request_event(
        self,
        *,
        skill_ids: list[str] | tuple[str, ...],
        retry_text: str,
        label: str = "",
        capability_id: str = "",
    ) -> dict[str, Any]:
        skills: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in skill_ids:
            sid = str(raw or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            skills.append(self._scp_skill_catalog_meta(sid))
        titles = "、".join(item["title"] for item in skills) or (label or "对应科研能力")
        primary = skills[0] if skills else {"skill_id": "", "title": titles, "description": ""}
        return {
            "type": "install_request",
            "kind": "scp_skill",
            "skills": skills,
            "skill_id": str(primary.get("skill_id") or ""),
            "title": str(primary.get("title") or titles),
            "description": str(primary.get("description") or ""),
            "label": str(label or titles),
            "capability_id": str(capability_id or ""),
            "retry_text": str(retry_text or ""),
            "summary": f"需要安装「{titles}」后才能继续；安装成功即可在当前对话使用。",
        }

    def _yield_scp_install_request(
        self,
        session: AgentSession,
        *,
        skill_ids: list[str] | tuple[str, ...],
        retry_text: str,
        label: str = "",
        capability_id: str = "",
        out: Any | None = None,
    ):
        """Emit floating install-request card; do not fall back to settings guidance."""
        emit = out or (lambda event: self._emit(session, event))
        payload = self._install_request_event(
            skill_ids=skill_ids,
            retry_text=retry_text,
            label=label,
            capability_id=capability_id,
        )
        titles = "、".join(
            str(item.get("title") or item.get("skill_id") or "")
            for item in (payload.get("skills") or [])
            if isinstance(item, dict)
        ) or str(payload.get("label") or "对应科研能力")
        reply = (
            f"本轮需要「{titles}」。请在安装请求卡片中确认；"
            "安装成功后即可在当前对话继续，无需重新发送原请求。"
        )
        session.messages.append({"role": "assistant", "text": reply})
        yield emit(payload)
        yield emit({"type": "assistant", "text": reply})
        yield emit({"type": "done", "status": "succeeded"})
        self.store.persist(session)

    def _clarify_reply_for_route(self, task_route: Any) -> str:
        reason = str(getattr(task_route, "reason", "") or "").strip()
        label = str(getattr(task_route, "label", "") or "").strip()
        if reason == "ranking_followup_missing_frozen_result":
            return (
                "当前会话还没有可用的冻结筛选结果，无法解释排名。"
                "请先完成一轮筛选，或指明具体分子 ID 后再问。"
            )
        if reason.startswith("scp_skill_not_installed:"):
            skill_id = reason.split(":", 1)[-1].strip() or "对应科研 Skill"
            meta = self._scp_skill_catalog_meta(skill_id)
            title = label or meta["title"] or "该科研能力"
            return (
                f"本轮需要「{title}」（`{meta['skill_id'] or skill_id}`）。"
                "请确认安装请求卡片；安装成功后即可在当前对话继续使用，不会用其它 Skill 代替。"
            )
        if reason.startswith("execution_gate_block:clarify") or reason == (
            "execution_gate_block:clarify"
        ):
            return (
                "还需要确认一下：本轮是要现在执行筛选/导出，还是先只讨论条件？"
                "请直接说明目标（例如「用默认配置筛选 Top20」或「先别跑，只讨论」）。"
            )
        if reason.startswith("execution_gate_block:"):
            return (
                "本轮先不调用筛选或导出工具。"
                "若要继续讨论条件，直接说；若要执行，请明确提出筛选/导出请求。"
            )
        if reason and not reason.startswith(
            ("structured_", "frozen_", "ranking_", "no_", "tool_", "execution_gate_", "scp_")
        ):
            return reason
        if label:
            return f"{label}。请补充相关信息后再试。"
        return "还需要补充一些信息才能继续，请说得更具体一些。"

    def _deny_reply_for_route(self, task_route: Any) -> str:
        reason = str(getattr(task_route, "reason", "") or "").strip()
        label = str(getattr(task_route, "label", "") or "").strip()
        if reason == "frozen_ranking_boundary_scp_cannot_rewrite_selection":
            return (
                "冻结候选的排序只能由 MolMind Core 的筛选路径改写；"
                "补充资料（含 SCP）不能重算或改写主榜。若要重筛，请明确请求 Core 筛选/导出。"
            )
        if label and reason:
            return f"{label}：{reason}"
        if reason:
            return f"该请求未被执行：{reason}"
        if label:
            return f"该请求被拒绝：{label}"
        return "该请求被科研治理边界拒绝，未执行。"

    def _classify_request_action(
        self, session: AgentSession, text: str, intent: AgentIntent
    ) -> tuple[str, str]:
        """Classify execute-vs-chat before a tool-shaped request is dispatched."""
        if frozen_ranking_mutation_requested(text):
            return "chat", "frozen_ranking_boundary_scp_cannot_rewrite_selection"
        # Online: LLM decides execute vs explain vs chat. ranking_question_fallback
        # is only used when the model is unavailable (see llm_ branch below).
        planned, plan_status = llm_plan_request(
            text=text,
            recent_messages=session.messages,
            tools=self.registry.tools,
            skills=self.registry.skills,
            capabilities=session_capabilities(session),
            default_top_n=session.top_n,
            attachment_context=self._attachment_context_text(session),
            has_sdf=bool(session.sdf_bytes),
            sdf_filename=str(session.sdf_filename or ""),
        )
        if planned is not None:
            if planned.action == "execute":
                return "execute_tools", f"{plan_status};{planned.rationale}"
            if planned.action == "explain":
                return "explain_ranking", f"{plan_status};{planned.rationale}"
            if planned.action == "clarify":
                missing_slots: list[str] = []
                if not session.sdf_bytes and (
                    intent.want_csv
                    or intent.want_pdf
                    or intent.want_reserve
                    or intent.want_bundle
                    or intent.force_rescreen
                    or intent.execution_requested
                ):
                    missing_slots.append("sdf")
                session.pending_goal = {
                    "goal": planned.goal,
                    "rationale": planned.rationale,
                    "source_text": text,
                    "reason": "tool_contract_missing_parameters",
                    "missing_slots": missing_slots,
                }
                # Persist an executable slot wait when the only blocker is SDF,
                # so short follow-ups resume instead of falling through to chat.
                if (
                    not session.sdf_bytes
                    and intent.wants_tools
                    and (
                        intent.want_csv
                        or intent.want_pdf
                        or intent.want_reserve
                        or intent.want_bundle
                        or intent.force_rescreen
                        or intent.execution_requested
                    )
                    and not (
                        isinstance(session.pending_action, dict)
                        and session.pending_action
                    )
                ):
                    top_n_val = (
                        int(intent.top_n)
                        if intent.requested_top_n is not None
                        or intent.execution_requested
                        else None
                    )
                    missing = ["sdf"]
                    if top_n_val is None:
                        missing.append("top_n")
                    session.pending_action = {
                        "kind": "deliverable",
                        "status": "awaiting_slots",
                        "want_csv": bool(intent.want_csv)
                        or bool(intent.execution_requested),
                        "want_pdf": bool(intent.want_pdf),
                        "want_reserve": bool(intent.want_reserve),
                        "want_bundle": bool(intent.want_bundle),
                        "top_n": top_n_val,
                        "requested_top_n": intent.requested_top_n,
                        "skill_ids": list(intent.skill_ids or ("masld_nominate",)),
                        "source_text": text,
                        "missing_slots": missing,
                    }
            return "chat", f"{plan_status};{planned.rationale}"

        history_lines: list[str] = []
        for message in session.messages[-6:]:
            role = str(message.get("role") or "")
            body = str(message.get("text") or "").strip()
            if role in {"user", "assistant"} and body:
                history_lines.append(f"{role}: {body[:500]}")
        history = "\n".join(history_lines) if history_lines else "（无）"
        frozen_runs = ", ".join(
            f"{entry.get('run_id', '')}:Top{entry.get('top_n', '')}"
            for entry in session.run_history[-4:]
            if entry.get("run_id")
        ) or "（无持久化运行摘要）"
        decision, why = llm_json_decision(
            system=(
                "你是 MolMind 的对话动作分类器。只返回 JSON："
                '{"decision":"execute_tools|explain_ranking|chat","reason":"..."}。'
                "execute_tools=用户明确要求生成/导出/运行筛选或报告；"
                "explain_ranking=用户在追问已有候选为何排名、入选或被淘汰，"
                "此类请求只解释冻结结果，不重新筛选；"
                "chat=普通知识问答、澄清或能力咨询。"
                "必须结合上下文判断：例如“top1”可以是输出数量，也可以是被询问的排名对象。"
                "已有冻结结果时，指向具体名次/分子并追问原因或对比的请求属于 explain_ranking。"
                "不要因为出现 plugin、skill、tool、top、候选、csv 等词就自动执行。"
            ),
            user=(
                f"已有筛选结果：{'有' if session.last_result is not None else '无'}；"
                f"最近冻结主榜数量：{len(getattr(session.last_result, 'top_molecules', None) or []) if session.last_result is not None else 0}；"
                f"最近冻结运行：{frozen_runs}；"
                f"结构解析出的候选动作：{intent.reason}\n"
                f"最近对话：\n{history}\n"
                f"用户本轮原文：{text}\n请判定本轮唯一 dialog action。"
            ),
            allowed={"execute_tools", "explain_ranking", "chat"},
            default="execute_tools",
            # Reuse the conversational model switch; intent classification is
            # part of the agent turn, not a separate mechanism/critic model.
            purpose="agent_chat",
            max_tokens=160,
            timeout_sec=8.0,
        )
        if why.startswith("llm_"):
            # No model: retain a narrow, read-only safety fallback for the
            # known ambiguity. This is not the primary routing policy.
            is_question, _ = ranking_question_fallback(text)
            if is_question:
                return "explain_ranking", f"{why};structural_question_fallback"
            return "execute_tools", why
        if decision in {"execute_tools", "explain_ranking", "chat"}:
            return decision, why
        return "execute_tools", why

    def _classify_pending_continuation(
        self,
        session: AgentSession,
        text: str,
        pending: dict[str, Any],
    ) -> tuple[str, str]:
        """Classify a short reply against an unfinished pending_action.

        Returns ``continue|status|other``. Cancel and bare TopN are handled by
        structural regexes before this classifier. Affirm/status synonym tables
        are offline-only defaults.
        """
        compact = str(text or "").strip()
        if _PENDING_STATUS_RE.search(compact):
            offline = "status"
        elif _PENDING_AFFIRM_RE.fullmatch(compact):
            offline = "continue"
        else:
            offline = "other"
        missing = [
            slot
            for slot, present in (
                ("sdf", bool(session.sdf_bytes)),
                ("top_n", pending.get("top_n") is not None),
            )
            if not present
        ]
        decision, why = llm_json_decision(
            system=(
                "你在判断用户对「未完成的筛选/导出请求」的短回复。"
                "只返回 JSON：{\"decision\":\"continue|status|other\",\"reason\":\"...\"}。"
                "按语义原则判定，不要把表面用词当成口令白名单。"
                "continue=用户在续接该未完成请求（确认还要做、催促补齐槽位、表示可以继续）；"
                "status=用户在询问该请求是否已开始/完成/进度，而不是另起新话题；"
                "other=取消以外的新需求、无关闲聊，或无法判断。"
                "取消类短句由上游协议处理，不会进入本分类器。"
            ),
            user=(
                f"未完成请求来源：{str(pending.get('source_text') or '')[:240]}；"
                f"仍缺槽位：{', '.join(missing) or '无'}；"
                f"已记录 TopN：{pending.get('top_n')!r}。\n"
                f"用户本轮原文：{text}\n"
                "请判定 decision。"
            ),
            allowed={"continue", "status", "other"},
            default=offline,
            purpose="agent_chat",
            max_tokens=120,
            timeout_sec=6.0,
        )
        if why.startswith("llm_"):
            return offline, f"{why};structural_pending_fallback"
        if decision in {"continue", "status", "other"}:
            return decision, why
        return offline, why

    def _classify_top_confirm(
        self, text: str, pending: dict[str, Any]
    ) -> tuple[str, str]:
        """Return (affirm|negate|other, reason). Prefer LLM; else re-parse heuristics."""
        req = int(pending.get("requested_top_n") or 0)
        capped = int(pending.get("top_n") or pending.get("top_n_max") or 50)
        decision, why = llm_json_decision(
            system=(
                "你在判断用户是否同意把超出上限的 TopN 请求改为按上限输出。"
                "只返回 JSON：{\"decision\":\"affirm|negate|other\",\"reason\":\"...\"}。"
                "按语义原则判定，不要把表面用词当成口令白名单。"
                "affirm=同意按上限；negate=拒绝/取消；other=另起新需求或无法判断。"
            ),
            user=(
                f"先前用户要 Top{req}，系统上限 Top{capped}，已反问是否按上限输出。\n"
                f"用户本轮回复：{text}\n"
                "请判定 decision。"
            ),
            allowed={"affirm", "negate", "other"},
            default="other",
        )
        if decision in {"affirm", "negate"}:
            return decision, why

        # No rigid keyword table: if user restates a valid in-bound tool request, treat as other.
        lo = int(pending.get("top_n_min") or 1)
        hi = int(pending.get("top_n_max") or capped)
        probe = parse_intent(text, default_top_n=capped, top_n_min=lo, top_n_max=hi)
        if probe.wants_tools and not probe.top_n_over_limit:
            return "other", f"{why};reparse_as_new_request"
        if probe.wants_tools and probe.top_n_over_limit:
            return "other", f"{why};still_over_limit"
        return "other", why

    @staticmethod
    def _compact_observation(value: object, *, limit: int = 1800) -> str:
        """Bound a branch observation before memory/context injection."""
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        head = max(1, int(limit * 0.72))
        tail = max(1, limit - head - 24)
        return f"{text[:head]}\n…[中间内容已压缩]…\n{text[-tail:]}"

    @staticmethod
    def _tool_call_memory(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        for event in events:
            kind = str(event.get("type") or "")
            tool = str(event.get("tool") or "")
            if kind == "tool_start" and tool:
                calls.append(
                    {
                        "tool": tool,
                        "plugin": str(event.get("plugin") or ""),
                        "args": _safe_query_value(event.get("args") or {}),
                        "status": "running",
                    }
                )
                continue
            if kind != "tool_end" or not tool:
                continue
            target = next(
                (
                    item
                    for item in reversed(calls)
                    if item.get("tool") == tool and item.get("status") == "running"
                ),
                None,
            )
            if target is None:
                target = {"tool": tool, "args": {}, "status": "running"}
                calls.append(target)
            target["status"] = "succeeded" if bool(event.get("ok")) else "failed"
            target["observation"] = {
                "digest": _safe_query_value(event.get("digest") or {}),
                "error": _redact_query_text(event.get("error") or "", limit=400),
            }
        return calls[-12:]

    def _decide_loop_after_observations(
        self,
        *,
        objective: str,
        iteration: int,
        tool_calls: list[dict[str, Any]],
        mention_observation: str,
        chat_observation: str,
        synthesis_observation: str = "",
        max_iterations: int = _COMPOUND_MAX_ITERATIONS,
    ) -> tuple[str, str]:
        """Choose continue/final/clarify/abort after branch observations."""
        has_failure = any(call.get("status") == "failed" for call in tool_calls)
        default = "clarify" if has_failure else "final"
        if not mention_observation and not tool_calls:
            default = "continue"
        if not chat_observation:
            default = "continue"
        decision, why = llm_json_decision(
            system=(
                "你是 MolMind Agent Loop 的停止条件判定器。只返回 JSON："
                '{"decision":"continue|final|clarify|abort","reason":"..."}。'
                "final=本轮多个子任务已有足够结果，可合并输出；"
                "continue=现有观察足以支持再做一轮内部推理或纠偏，且不需要用户补充；"
                "clarify=缺少附件、参数、授权或用户选择，必须询问用户；"
                "abort=安全门禁拒绝或不可恢复错误。"
                "不要因为存在工具调用就忽略普通问答，也不要在结果已经足够时继续循环。"
            ),
            user=(
                f"目标：{self._compact_observation(objective, limit=700)}\n"
                f"当前迭代：{iteration}/{max_iterations}\n"
                f"工具调用：{json.dumps(tool_calls, ensure_ascii=False)[:3500]}\n"
                f"点选分支观察：{self._compact_observation(mention_observation)}\n"
                f"对话分支观察：{self._compact_observation(chat_observation)}\n"
                f"上一轮综合观察：{self._compact_observation(synthesis_observation)}\n"
                "判断是进入下一轮还是输出。"
            ),
            allowed={"continue", "final", "clarify", "abort"},
            default=default,
            purpose="agent_chat",
            max_tokens=180,
            timeout_sec=8.0,
        )
        return decision, why

    def _merge_compound_reply(
        self,
        *,
        objective: str,
        companion_text: str,
        mention_observation: str,
        chat_observation: str,
        tool_calls: list[dict[str, Any]],
        decision: str,
    ) -> str:
        """Merge parallel branch results without replaying verbose tool output."""
        mention_short = self._compact_observation(mention_observation, limit=2400)
        chat_short = self._compact_observation(chat_observation, limit=2400)
        try:
            from plugins.molmind_core.scientific.mechanism.llm_client import (
                chat_completion,
                resolve_llm_settings,
            )

            settings = resolve_llm_settings(
                {"enabled": True, "agent_chat": True},
                purpose="agent_chat",
            )
            if settings.ready:
                settings = type(settings)(
                    enabled=settings.enabled,
                    model=settings.model,
                    base_url=settings.base_url,
                    api_key=settings.api_key,
                    temperature=0.2,
                    timeout_sec=min(max(settings.timeout_sec, 20.0), 45.0),
                    max_tokens=min(max(settings.max_tokens, 800), 1600),
                    cache_dir=settings.cache_dir,
                    use_cache=False,
                )
                system = (
                    "你是 MolMind 的结果综合器。把同一用户输入的点选工具/技能分支与"
                    "普通对话分支合并成一条简洁中文答复。保留成功结果、失败/缺参事实和"
                    "科学声明边界；不要虚构工具结果，不要重复完整日志或大表。"
                    "工具卡片和下载附件已在界面单独展示，只需在正文概括。"
                )
                user = (
                    f"用户完整目标：{self._compact_observation(objective, limit=900)}\n"
                    f"普通问答子任务：{companion_text}\n"
                    f"Loop 决策：{decision}\n"
                    f"工具调用摘要：{json.dumps(tool_calls, ensure_ascii=False)[:3200]}\n"
                    f"点选分支结果：{mention_short or '（无文本结果）'}\n"
                    f"普通对话结果：{chat_short or '（无可用回答）'}\n"
                    "请输出合并后的最终答复。"
                )
                merged = chat_completion(settings, system=system, user=user).strip()
                if merged:
                    return self._append_degraded_disclosure(merged, tool_calls)
        except Exception:  # noqa: BLE001 - optional synthesis model
            pass

        parts: list[str] = []
        if mention_short:
            parts.append(f"点选项处理结果：\n{mention_short}")
        elif tool_calls:
            status = "、".join(
                f"{call.get('tool')}={call.get('status')}" for call in tool_calls
            )
            parts.append(f"点选项处理结果：{status}")
        if chat_short:
            parts.append(f"关于“{companion_text}”：\n{chat_short}")
        if decision == "clarify" and not any(
            marker in "\n".join(parts) for marker in ("请先", "需要", "缺少", "无法")
        ):
            parts.append("还缺少继续执行所需的信息，请补充后我再继续。")
        if decision == "abort":
            parts.append("本轮已因安全门禁或不可恢复错误停止。")
        fallback = "\n\n".join(parts) or "本轮没有得到可安全输出的结果。"
        return self._append_degraded_disclosure(fallback, tool_calls)

    @staticmethod
    def _append_degraded_disclosure(
        reply: str,
        tool_calls: list[dict[str, Any]],
    ) -> str:
        """Ensure synthesis cannot hide a Tool's degraded evidence channel."""
        channels: list[str] = []
        for call in tool_calls:
            observation = call.get("observation") or {}
            digest = observation.get("digest") or {}
            for channel in [
                *(observation.get("degraded_channels") or []),
                *(digest.get("degraded_channels") or []),
            ]:
                value = str(channel or "").strip()
                if value and value not in channels:
                    channels.append(value)
        if not channels:
            return reply
        if all(channel in reply for channel in channels):
            return reply
        descriptions = {
            "evidence_provenance_incomplete": "部分证据的来源溯源字段不完整",
        }
        details = "；".join(
            f"`{channel}`（{descriptions.get(channel, '该证据通道处于降级状态')}）"
            for channel in channels
        )
        return (
            f"{reply.rstrip()}\n\n"
            f"注意：本轮工具报告了降级通道：{details}。"
            "这不代表没有证据，但解读时应结合证据卡中的来源与审计字段。"
        )

    def _remember_loop_iteration(
        self,
        session: AgentSession,
        *,
        turn_id: str,
        iteration: int,
        objective: str,
        intent: AgentIntent,
        tool_calls: list[dict[str, Any]],
        mention_observation: str,
        chat_observation: str,
        synthesis_observation: str,
        decision: str,
        reason: str,
    ) -> None:
        session.working_memory.append(
            {
                "turn_id": turn_id,
                "iteration": iteration,
                "objective": self._compact_observation(objective, limit=700),
                "tasks": [
                    {
                        "kind": "mention",
                        "targets": [
                            {"kind": item.kind, "id": item.id}
                            for item in intent.mentions
                        ],
                        "action": intent.mention_action,
                        "status": (
                            "failed"
                            if any(call.get("status") == "failed" for call in tool_calls)
                            else "completed"
                        ),
                    },
                    {
                        "kind": "conversation",
                        "text": self._compact_observation(
                            intent.companion_text,
                            limit=500,
                        ),
                        "status": "completed" if chat_observation else "failed",
                    },
                ],
                "tool_calls": copy.deepcopy(tool_calls),
                "observations": {
                    "mention": self._compact_observation(mention_observation),
                    "conversation": self._compact_observation(chat_observation),
                    "synthesis": self._compact_observation(synthesis_observation),
                },
                "decision": decision,
                "reason": self._compact_observation(reason, limit=500),
                "recorded_at_unix": int(time.time()),
            }
        )
        session.working_memory = session.working_memory[-24:]
        # Snapshot after every decision so a long/streaming turn can be
        # inspected or resumed even if the final synthesis is interrupted.
        self.store.persist(session)

    def _handle_compound_turn(
        self,
        session: AgentSession,
        text: str,
        intent: AgentIntent,
    ) -> Iterator[dict[str, Any]]:
        """Run mention and conversational tasks concurrently, then close the loop."""
        self._prepare_turn(session, text)
        turn_id = uuid.uuid4().hex[:12]
        yield self._emit(
            session,
            {
                "type": "agent_plan",
                "goal": text,
                "action": "execute",
                "tasks": [
                    {
                        "task_id": "mention",
                        "kind": "mention",
                        "label": "处理点选工具或技能",
                        "depends_on": [],
                    },
                    {
                        "task_id": "conversation",
                        "kind": "conversation",
                        "label": intent.companion_text,
                        "depends_on": [],
                    },
                    {
                        "task_id": "synthesis",
                        "kind": "synthesis",
                        "label": "合并观察并决定是否结束",
                        "depends_on": ["mention", "conversation"],
                    },
                ],
                "expected_artifacts": [],
                "diagnostics": [],
            },
        )
        yield self._emit(
            session,
            {
                "type": "plan",
                "steps": [
                    "并行处理点选工具/技能与普通问答",
                    "汇总并压缩各分支 Observation",
                    "判断继续 Loop、澄清、终止或输出",
                ],
            },
        )
        yield self._emit(
            session,
            {
                "type": "thinking",
                "text": (
                    "识别到同一输入包含点选任务和普通对话任务；两部分都会处理，"
                    "完成后由 Agent Loop 基于观察结果统一决策。"
                ),
            },
        )

        yield self._emit(session, {"type": "task_start", "task_id": "mention"})
        yield self._emit(session, {"type": "task_start", "task_id": "conversation"})
        chat_future = self._branch_executor.submit(
            self._llm_chat_reply,
            session,
            intent.companion_text,
        )
        captured_assistant: list[str] = []
        branch_events: list[dict[str, Any]] = []
        capture_token = _BRANCH_ASSISTANT_CAPTURE.set(captured_assistant)
        try:
            for event in self._handle_mentions(
                session,
                intent,
                finalize=False,
                compound=True,
            ):
                if event.get("type") == "branch_observation":
                    continue
                branch_events.append(event)
                yield event
        finally:
            _BRANCH_ASSISTANT_CAPTURE.reset(capture_token)

        try:
            # The conversational client defaults to a 60s network timeout;
            # allow a small scheduling margin before treating the branch as a
            # failed observation.
            chat_observation = str(chat_future.result(timeout=70.0) or "").strip()
        except Exception as exc:  # noqa: BLE001 - branch failure becomes observation
            chat_observation = ""
            branch_events.append(
                {
                    "type": "branch_error",
                    "branch": "conversation",
                    "error": type(exc).__name__,
                }
            )

        mention_observation = "\n\n".join(
            block for block in captured_assistant if str(block).strip()
        )
        tool_calls = self._tool_call_memory(branch_events)
        mention_failed = any(call.get("status") == "failed" for call in tool_calls)
        yield self._emit(
            session,
            {
                "type": "task_end",
                "task_id": "mention",
                "status": "failed" if mention_failed else "succeeded",
                "observation": {
                    "summary": self._compact_observation(mention_observation),
                    "tool_calls": tool_calls,
                },
            },
        )
        yield self._emit(
            session,
            {
                "type": "task_end",
                "task_id": "conversation",
                "status": "succeeded" if chat_observation else "failed",
                "observation": {
                    "summary": self._compact_observation(chat_observation),
                },
            },
        )
        yield self._emit(session, {"type": "task_start", "task_id": "synthesis"})
        synthesis_observation = ""
        seen_continue_signatures: set[str] = set()
        decision = "final"
        reason = "bounded_loop_default"
        loop_limit = min(
            _COMPOUND_MAX_ITERATIONS,
            self._run_controller(session).budget.max_iterations,
        )

        for iteration in range(1, loop_limit + 1):
            decision, reason = self._decide_loop_after_observations(
                objective=text,
                iteration=iteration,
                tool_calls=tool_calls,
                mention_observation=mention_observation,
                chat_observation=chat_observation,
                synthesis_observation=synthesis_observation,
                max_iterations=loop_limit,
            )
            signature = content_sha256(
                json.dumps(
                    {
                        "decision": decision,
                        "tools": tool_calls,
                        "mention": self._compact_observation(mention_observation),
                        "chat": self._compact_observation(chat_observation),
                        "synthesis": self._compact_observation(synthesis_observation),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            if decision == "continue" and signature in seen_continue_signatures:
                decision = "final"
                reason = f"{reason};loop_stalled"
            elif decision == "continue":
                seen_continue_signatures.add(signature)
            if decision == "continue" and iteration >= loop_limit:
                decision = "final"
                reason = f"{reason};max_iterations_reached"

            self._remember_loop_iteration(
                session,
                turn_id=turn_id,
                iteration=iteration,
                objective=text,
                intent=intent,
                tool_calls=tool_calls,
                mention_observation=mention_observation,
                chat_observation=chat_observation,
                synthesis_observation=synthesis_observation,
                decision=decision,
                reason=reason,
            )
            yield self._emit(
                session,
                {
                    "type": "loop_decision",
                    "turn_id": turn_id,
                    "iteration": iteration,
                    "decision": decision,
                    "reason": self._compact_observation(reason, limit=500),
                    "max_iterations": loop_limit,
                },
            )
            if decision != "continue":
                break
            synthesis_observation = self._merge_compound_reply(
                objective=text,
                companion_text=intent.companion_text,
                mention_observation=mention_observation,
                chat_observation=chat_observation,
                tool_calls=tool_calls,
                decision="continue",
            )

        reply = synthesis_observation or self._merge_compound_reply(
            objective=text,
            companion_text=intent.companion_text,
            mention_observation=mention_observation,
            chat_observation=chat_observation,
            tool_calls=tool_calls,
            decision=decision,
        )
        yield self._emit(
            session,
            {
                "type": "task_end",
                "task_id": "synthesis",
                "status": "succeeded",
                "observation": {
                    "decision": decision,
                    "reason": self._compact_observation(reason, limit=500),
                },
            },
        )
        yield self._emit(session, {"type": "assistant", "text": reply})
        yield self._emit(session, {"type": "done"})
        self.store.persist(session)

    def _prepare_turn(self, session: AgentSession, text: str) -> None:
        """Append one user message and consume any pending UI-only attachment."""
        turn_attachments: list[dict[str, Any]] = []
        active = session.active_run or {}
        for summary in active.get("attachment_summaries") or []:
            if isinstance(summary, dict):
                turn_attachments.append(copy.deepcopy(summary))
        # Direct-send / legacy path: SDF may only be flagged via sdf_ui_pending.
        if session.sdf_ui_pending and session.sdf_filename and session.sdf_bytes:
            already = any(
                str(item.get("kind") or "") == "sdf"
                or str(item.get("filename") or "").lower().endswith(".sdf")
                for item in turn_attachments
            )
            if not already:
                turn_attachments.append(
                    {"kind": "sdf", "filename": session.sdf_filename}
                )
            session.sdf_ui_pending = False
        display_text = str(active.get("display_text") or text)
        message = {
            "role": "user",
            "text": display_text,
            "attachments": turn_attachments,
            "created_at": str(active.get("started_at") or "") or utc_now(),
            "run_id": str(active.get("run_id") or ""),
            "turn_id": str(active.get("turn_id") or ""),
        }
        if active.get("kind") == "guidance":
            message.update(
                {
                    "kind": "guidance",
                    "parent_run_id": str(active.get("parent_run_id") or ""),
                }
            )
        session.messages.append(message)
        if not session.title:
            self.store.set_title(session, text)
        else:
            self.store.persist(session)

    def _handle_intent(
        self, session: AgentSession, text: str, intent: Any
    ) -> Iterator[dict[str, Any]]:
        # A Top-N token inside a question is not a new session preference.
        # Update it only for an actual nomination/report execution intent.
        if intent.want_csv or intent.want_pdf:
            session.top_n = intent.top_n
        self._prepare_turn(session, text)

        def out(ev: dict[str, Any]) -> dict[str, Any]:
            return self._emit(session, ev)

        has_sdf = bool(session.sdf_bytes)
        ensure_session_last_result(session)
        has_result = session.last_result is not None

        task_route = self.task_router.route(intent, session)
        yield out(
            {
                "type": "thinking",
                "text": (
                    f"路由：{task_route.route}"
                    + (f"/{task_route.capability_id}" if task_route.capability_id else "")
                    + f"（{task_route.reason}）"
                ),
            }
        )

        if task_route.route == "deny":
            reply = self._deny_reply_for_route(task_route)
            session.messages.append({"role": "assistant", "text": reply})
            yield out({"type": "assistant", "text": reply})
            yield out({"type": "done", "status": "succeeded"})
            self.store.persist(session)
            return

        if task_route.route == "clarify":
            reason = str(getattr(task_route, "reason", "") or "")
            if reason.startswith("scp_skill_not_installed:"):
                skill_id = (
                    str(getattr(task_route, "skill_id", "") or "").strip()
                    or reason.split(":", 1)[-1].strip()
                )
                yield from self._yield_scp_install_request(
                    session,
                    skill_ids=[skill_id],
                    retry_text=text,
                    label=str(getattr(task_route, "label", "") or ""),
                    capability_id=str(getattr(task_route, "capability_id", "") or ""),
                    out=out,
                )
                return
            reply = self._clarify_reply_for_route(task_route)
            session.messages.append({"role": "assistant", "text": reply})
            yield out({"type": "assistant", "text": reply})
            yield out({"type": "done", "status": "succeeded"})
            self.store.persist(session)
            return

        # Live SCP only when the unified TaskRouter selected the scp lane.
        if task_route.route == "scp":
            scp_handled = yield from self._maybe_run_scp_chat(session, text)
            if scp_handled:
                return

        if task_route.route == "explain":
            intent = replace(
                intent,
                explain_ranking=True,
                wants_tools=False,
                want_csv=False,
                want_pdf=False,
                skill_ids=(),
                reason="询问上一轮候选排名原因，不重新筛选或导出",
            )

        # / @ 点选：单独介绍或试用，不联动整条筛选流水线
        if intent.mentions and intent.mention_action:
            yield out(
                {
                    "type": "agent_plan",
                    "goal": text,
                    "action": (
                        "execute" if intent.mention_action == "invoke" else "explain"
                    ),
                    "tasks": [
                        {
                            "task_id": "mention",
                            "kind": "mention",
                            "label": "处理点选工具、技能或插件",
                            "depends_on": [],
                        }
                    ],
                    "expected_artifacts": [],
                    "diagnostics": [],
                }
            )
            yield out({"type": "task_start", "task_id": "mention"})
            failed = False
            for event in self._handle_mentions(session, intent, finalize=False):
                if event.get("type") == "tool_end" and not event.get("ok"):
                    failed = True
                yield event
            yield out(
                {
                    "type": "task_end",
                    "task_id": "mention",
                    "status": "failed" if failed else "succeeded",
                    "observation": {
                        "summary": (
                            "点选调用未完成" if failed else "点选任务已处理"
                        )
                    },
                }
            )
            yield out({"type": "done"})
            self.store.persist(session)
            return

        # 自然语言证据查询是独立只读 Tool，不触发筛选、导出或 Catalog。
        if intent.query_evidence:
            if not isinstance(session.active_plan, dict):
                yield out(
                    {
                        "type": "agent_plan",
                        "goal": text,
                        "action": "execute",
                        "steps": [
                            {
                                "task_id": "query-evidence",
                                "kind": "tool",
                                "tool": "query_evidence",
                                "args": {},
                                "depends_on": [],
                            }
                        ],
                        "expected_artifacts": ["evidence_card"],
                        "diagnostics": [],
                    }
                )
            yield from self._run_query_evidence(session, intent)
            yield out({"type": "done"})
            self.store.persist(session)
            return

        if (
            intent.wants_tools
            and isinstance(session.pending_goal, dict)
            and (
                _DEFAULT_CONFIG_EXECUTION_RE.search(text)
                or re.search(r"忽略(?:上述|之前)?(?:条件|偏好)", text, re.I)
            )
        ):
            # Explicit user confirmation is the only way to discard an
            # unexecutable constraint set and run the default scientific path.
            session.pending_goal = None

        if intent.wants_tools and isinstance(session.pending_goal, dict):
            pending = session.pending_goal
            missing_slots = [
                str(slot)
                for slot in (pending.get("missing_slots") or [])
                if str(slot)
            ]
            # Stale clarify that only waited on the library: SDF is now bound.
            if missing_slots == ["sdf"] and session.sdf_bytes:
                session.pending_goal = None
            else:
                reply = (
                    "你此前提出的筛选条件尚未映射为当前工具的可执行参数，因此我没有启动筛选。"
                    f"待确认目标：{pending.get('goal') or pending.get('source_text') or '筛选条件'}。\n\n"
                    "请明确选择：\n"
                    "1. 使用当前默认 MASLD 筛选配置生成 TopN；或\n"
                    "2. 继续只讨论这些条件；或\n"
                    "3. 提供已支持的参数配置后再执行。\n\n"
                    "在你确认前，我不会把这些条件静默替换成默认筛选。"
                )
                session.messages.append({"role": "assistant", "text": reply})
                yield out({"type": "assistant", "text": reply})
                yield out({"type": "done"})
                self.store.persist(session)
                return

        # 纯对话：用 LLM 回答（不强制 SDF）；失败再降级模板
        if not intent.wants_tools:
            yield out(
                {
                    "type": "agent_plan",
                    "goal": text,
                    "action": "chat",
                    "tasks": [
                        {
                            "task_id": "conversation",
                            "kind": "conversation",
                            "label": "生成对话回复",
                            "depends_on": [],
                        }
                    ],
                    "expected_artifacts": [],
                    "diagnostics": [],
                }
            )
            yield out({"type": "task_start", "task_id": "conversation"})
            yield out(
                {
                    "type": "plan",
                    "steps": ["理解问题", "生成对话回复"],
                }
            )
            yield out(
                {
                    "type": "thinking",
                    "text": "识别为一般问答，准备用对话模型回复。",
                }
            )
            reply = None
            streamed = False
            if intent.explain_ranking:
                ensure_session_last_result(session)
                reply = format_ranking_explanation(
                    session.last_result,
                    molecule_id=intent.ranking_molecule_id,
                    rank_limit=(
                        intent.requested_top_n
                        if intent.ranking_molecule_id is None
                        else None
                    ),
                    rank_positions=intent.ranking_positions,
                    rank_position_subject=intent.ranking_position_subject,
                )
                if reply is None and session.last_result is None:
                    reply = (
                        "当前会话还没有可用的冻结筛选结果，无法解释排名。"
                        "请先完成一轮筛选，或指明具体分子 ID 后再问。"
                    )
            if not reply:
                parts: list[str] = []
                try:
                    for delta in self._llm_chat_reply_stream(session, text):
                        piece = str(delta or "")
                        if not piece:
                            continue
                        parts.append(piece)
                        streamed = True
                        yield self._emit_live(
                            session,
                            {"type": "assistant_delta", "delta": piece},
                        )
                    reply = "".join(parts).strip()
                except CallCancelled:
                    raise
                except RunInterrupted:
                    raise
                except Exception as exc:  # noqa: BLE001 — LLM optional
                    if self._run_controller(session).interruption_requested:
                        raise RunInterrupted("user_guidance") from exc
                    reply = ""
                if not reply:
                    reply = self._chat_reply_fallback(session, text)
                    streamed = False
            session.messages.append({"role": "assistant", "text": reply})
            yield out(
                {
                    "type": "task_end",
                    "task_id": "conversation",
                    "status": "succeeded",
                    "observation": {
                        "summary": self._compact_observation(reply, limit=1200),
                        "streamed": streamed,
                    },
                }
            )
            yield out({"type": "assistant", "text": reply})
            yield out({"type": "done"})
            self.store.persist(session)
            return

        # Top N 超规范上限：反问，不静默截断开跑
        if intent.top_n_over_limit and intent.requested_top_n is not None:
            req = int(intent.requested_top_n)
            hi = int(intent.top_n_max)
            lo = int(intent.top_n_min)
            limit_src = "、".join(intent.skill_ids) or "masld_nominate / score_and_rank"
            yield out(
                {
                    "type": "thinking",
                    "text": (
                        f"用户要求 Top{req}，但技能/工具「{limit_src}」声明上限为 Top{hi}，"
                        "不能直接按超出上限的数量开跑，需先确认是否改为上限输出。"
                    ),
                }
            )
            reply = (
                f"相关插件/技能/工具限制最大为 Top{hi}，无法直接输出 Top{req}。"
                f"请问需要我按上限输出 Top{hi} 吗？"
            )
            session.pending_top_confirm = {
                "requested_top_n": req,
                "top_n": hi,
                "top_n_max": hi,
                "top_n_min": lo,
                "want_csv": bool(intent.want_csv),
                "want_pdf": bool(intent.want_pdf),
                "want_reserve": bool(intent.want_reserve),
                "want_bundle": bool(intent.want_bundle),
                "skill_ids": list(intent.skill_ids),
                "raw_text": intent.raw_text,
                "limit_source": limit_src,
            }
            session.messages.append({"role": "assistant", "text": reply})
            yield out({"type": "assistant", "text": reply})
            yield out({"type": "done"})
            self.store.persist(session)
            return

        frozen_primary_count = (
            len(getattr(session.last_result, "top_molecules", None) or [])
            if has_result
            else 0
        )
        if intent.want_pdf and has_result and not intent.want_csv and not intent.want_reserve and not intent.want_bundle:
            progress_text = (
                f"我会基于已冻结的 Top{frozen_primary_count} 候选生成机制与验证方案 PDF，"
                "不会重新筛选或改动排名。"
            )
        else:
            progress_text = (
                f"我会为你筛选 Top{intent.top_n} 候选，并整理成"
                + (" CSV 文件。" if intent.want_csv else "结果文件。")
                + "排名将依据固定的科学规则生成，确保结果可复核。"
                + (f" 将使用你上传的「{session.sdf_filename}」。" if has_sdf else " 还需要你先上传 SDF 化合物库。")
            )
        yield out({"type": "thinking", "text": progress_text})

        steps: list[str] = []
        if intent.want_csv or intent.want_reserve or intent.want_bundle or (intent.want_pdf and not has_result):
            steps.append(f"筛选并挑选最符合要求的 Top{intent.top_n} 候选")
            steps.append("整理候选分子 CSV")
        if intent.want_reserve:
            steps.append("从同一次冻结结果导出候补名单 CSV")
        if intent.want_pdf:
            steps.append("生成机制与后续验证建议 PDF")
        if intent.want_bundle:
            steps.append("打包候选清单、候补、运行清单与轨迹")
        steps.append("准备好下载结果")
        yield out({"type": "plan", "steps": steps})

        # A frozen Top10 cannot satisfy a later explicit Top15 request.  In
        # that case this is an execute_tools turn and must create a new frozen
        # result from the session SDF; merely exporting the old result would
        # produce a mislabeled, short CSV.  Conversely, explanation turns
        # have already exited above and never reach this gate.
        need_screen = (
            bool(getattr(intent, "force_rescreen", False))
            or (
                intent.want_csv
                and (
                    not has_result
                    or (
                        intent.requested_top_n is not None
                        and frozen_primary_count != intent.top_n
                    )
                )
            )
            or (
                (intent.want_reserve or intent.want_bundle or intent.want_pdf)
                and not has_result
            )
        )
        if getattr(intent, "force_rescreen", False):
            session.pending_goal = None
            session.pending_action = None
            session.pending_top_confirm = None
            if intent.requested_top_n is None:
                session.top_n = self._profile_default_top_n(session)
        if not need_screen and has_result:
            reused_task_id = self._task_id_for_tool(session, "score_and_rank")
            if reused_task_id:
                yield out(
                    {
                        "type": "task_end",
                        "task_id": reused_task_id,
                        "status": "skipped",
                        "observation": {
                            "reason": "reused_matching_frozen_result",
                            "run_id": str(
                                getattr(session.last_result, "run_id", "") or ""
                            ),
                            "top_n": frozen_primary_count,
                        },
                    }
                )
        if need_screen and not has_sdf:
            export_only = (
                (intent.want_pdf or intent.want_bundle or intent.want_reserve)
                and not intent.want_csv
                and not getattr(intent, "force_rescreen", False)
            )
            if export_only and not has_result:
                reply = (
                    "当前没有可用的冻结筛选结果，无法仅生成机制 PDF 或候选包。"
                    "请先完成一轮筛选；若化合物库已变更，请重新上传 SDF 后再筛选。"
                )
                session.messages.append({"role": "assistant", "text": reply})
                yield out({"type": "assistant", "text": reply})
                yield out({"type": "done", "status": "succeeded"})
                self.store.persist(session)
                return
            session.pending_action = {
                "kind": "deliverable",
                "status": "awaiting_slots",
                "want_csv": bool(intent.want_csv),
                "want_pdf": bool(intent.want_pdf),
                "want_reserve": bool(intent.want_reserve),
                "want_bundle": bool(intent.want_bundle),
                "top_n": (
                    int(intent.top_n) if intent.requested_top_n is not None else None
                ),
                "requested_top_n": intent.requested_top_n,
                "skill_ids": list(intent.skill_ids or ("masld_nominate",)),
                "source_text": intent.raw_text,
                "missing_slots": [
                    "sdf",
                    *([] if intent.requested_top_n is not None else ["top_n"]),
                ],
            }
            yield out(
                {
                    "type": "assistant",
                    "text": (
                        "这个需求需要化合物库才能跑筛选。"
                        "我已记住这次导出请求，请先在输入区上传 .sdf 附件。"
                        + (
                            "上传后告诉我候选数量（例如「10」），即可继续执行"
                            if intent.requested_top_n is None
                            else "上传后回复「继续」，即可按原请求执行"
                        )
                        + f"（目标示例：生成 top{intent.top_n} 候选清单 csv"
                        + ("，并给出机制 pdf" if intent.want_pdf else "")
                        + "）。"
                    ),
                }
            )
            yield out({"type": "done"})
            self.store.persist(session)
            return

        if need_screen:
            screen_ok = yield from self._execute_required_tool(
                session, "score_and_rank", {"top_n": intent.top_n}
            )
            if not screen_ok:
                yield out(
                    {
                        "type": "assistant",
                        "text": (
                            "计划中的筛选步骤未完成，因此我已停止后续导出和报告生成，"
                            "不会拿旧结果冒充本轮结果。请根据错误信息调整附件或条件后重试。"
                        ),
                    }
                )
                yield out({"type": "done"})
                self.store.persist(session)
                return

        if intent.want_csv:
            export_ok = yield from self._execute_required_tool(
                session,
                "export_nomination",
                {"tier": "primary"},
            )
            if not export_ok:
                yield out(
                    {
                        "type": "assistant",
                        "text": "主候选 CSV 未能生成，已停止依赖它的后续步骤；已有冻结排名未被改动。",
                    }
                )
                yield out({"type": "done"})
                self.store.persist(session)
                return
        if intent.want_reserve:
            reserve_ok = yield from self._execute_required_tool(
                session,
                "export_nomination",
                {"tier": "reserve"},
            )
            if not reserve_ok:
                yield out(
                    {
                        "type": "assistant",
                        "text": "候补 CSV 未能生成，已停止后续依赖步骤；主榜未被改动。",
                    }
                )
                yield out({"type": "done"})
                self.store.persist(session)
                return

        if intent.want_pdf:
            ensure_session_last_result(session)
            if session.last_result is None:
                yield out(
                    {
                        "type": "assistant",
                        "text": "还没有可用的筛选结果，无法生成机制 PDF。请先完成筛选，或上传 SDF 后重新发起。",
                    }
                )
                yield out({"type": "done"})
                self.store.persist(session)
                return
            mechanism_ok = yield from self._execute_required_tool(
                session,
                "start_mechanism_report",
                {},
            )
            if not mechanism_ok:
                yield out(
                    {
                        "type": "assistant",
                        "text": "机制报告未能启动或完成，已保留现有冻结候选和已生成产物。",
                    }
                )
                yield out({"type": "done"})
                self.store.persist(session)
                return

        if intent.want_bundle:
            bundle_ok = yield from self._execute_required_tool(
                session,
                "export_submission_bundle",
                {},
            )
            if not bundle_ok:
                yield out(
                    {
                        "type": "assistant",
                        "text": "结果归档包未能生成；已有冻结结果与单独产物仍保持不变。",
                    }
                )
                yield out({"type": "done"})
                self.store.persist(session)
                return

        # Catalog enrichment：仅在已主动添加时执行；失败降级，不改主榜
        if session.installed_catalog and session.last_result is not None:
            yield from self._run_catalog_enrichment(session)

        yield out(
            {
                "type": "assistant",
                "text": format_run_completion(
                    want_csv=bool(intent.want_csv),
                    want_pdf=bool(intent.want_pdf),
                    want_reserve=bool(intent.want_reserve),
                    want_bundle=bool(intent.want_bundle),
                    want_catalog=bool(
                        session.installed_catalog and session.last_result is not None
                    ),
                    result=session.last_result,
                ),
            }
        )
        yield out({"type": "done"})
        self.store.persist(session)

    @staticmethod
    def _scp_live_requested(text: str) -> bool:
        raw = str(text or "").strip().lower()
        if re.search(r"\ballow[_\s-]?live\s*[:=：]\s*(?:true|1|yes|on)\b", raw):
            return True
        return bool(
            re.search(
                r"(?:允许|开启|启用|使用|通过)\s*(?:实时|在线|联网|scp(?:\s|-)?hub|mcp)"
                r"|(?:实时|联网|在线)\s*(?:检索|查询|查文献|补充|调用)",
                raw,
                re.I,
            )
        )

    @staticmethod
    def _scp_live_disabled(text: str) -> bool:
        raw = str(text or "").lower()
        return bool(
            re.search(r"\ballow[_\s-]?live\s*[:=：]\s*(?:false|0|no|off)\b", raw)
            or re.search(r"(?:不要|别|禁止|不准|关闭|无需|不用)\s*(?:联网|实时|在线|scp(?:\s|-)?hub|mcp)", raw, re.I)
        )

    def _scp_plugin_default_live(self) -> bool:
        plugin = self.registry.plugins.get("scp-hub")
        policy = getattr(plugin, "network_policy", {}) if plugin else {}
        return bool(isinstance(policy, dict) and policy.get("default_live", False))

    def _scp_live_denied_reply(self, text: str) -> str:
        """Explain why SCP live was blocked; align copy with default_live policy."""
        if self._scp_live_disabled(text):
            return (
                "本轮消息已明确关闭联网，因此没有调用 SCP Hub，"
                "也不会把模型生成内容冒充实时结果。"
                "若要查询，请去掉「不要联网」等禁用表述，或写 `allow_live=true`。"
            )
        if self._scp_plugin_default_live():
            # default_live=true 时，通常只有显式禁用才会落到这里；兜底文案仍说明授权方式。
            return (
                "当前 SCP 插件默认允许联网，但本轮未获得有效联网授权，"
                "因此没有调用 SCP Hub，也不会把模型生成内容冒充实时结果。"
                "若要查询，请确认未写禁用联网，或显式写 `allow_live=true`。"
            )
        return (
            "这类问题可能需要实时科研资料；当前 SCP 插件默认不自动联网，"
            "且本轮消息未授权联网，因此没有调用 SCP Hub，"
            "也不会把模型生成内容冒充实时结果。"
            "若要查询，请明确写「允许联网」或 `allow_live=true`。"
        )

    @staticmethod
    def _scp_history_summary_requested(text: str) -> bool:
        """Offline-only fallback for reuse dialog act."""
        raw = str(text or "")
        return bool(
            re.search(r"(?:基于|根据|汇总|总结).{0,8}(?:刚才|上一轮|之前).{0,8}(?:证据|结果|查询)", raw)
            and not re.search(r"(?:重新|再次|再执行|重跑|重复)", raw)
        )

    @staticmethod
    def _scp_repeat_requested(text: str) -> bool:
        """Offline-only fallback for repeat dialog act."""
        return bool(
            re.search(
                r"(?:重新|再次|再执行|重跑|重复).{0,12}(?:相同|刚才|上一轮|之前).{0,12}(?:查询|机制|文献)",
                str(text or ""),
            )
        )

    @staticmethod
    def _scp_cache_report_requested(text: str) -> bool:
        """Offline-only fallback for cache_report dialog act."""
        raw = str(text or "").lower()
        return "缓存" in raw and bool(re.search(r"(?:命中|复用|哪些|来自|实时)", raw))

    def _classify_scp_dialog_act(
        self, session: AgentSession, text: str
    ) -> tuple[str, dict[str, Any]]:
        """LLM dialog act for SCP reuse/repeat/cache; regex only when LLM is down.

        Does not decide allow_live — that remains a code-enforced protocol gate.
        """
        offline_act = "execute"
        if self._scp_history_summary_requested(text):
            offline_act = "reuse"
        elif self._scp_repeat_requested(text):
            offline_act = "repeat"
        elif self._scp_cache_report_requested(text):
            offline_act = "cache_report"
        history_lines: list[str] = []
        for message in session.messages[-6:]:
            role = str(message.get("role") or "")
            body = str(message.get("text") or "").strip()
            if role in {"user", "assistant"} and body:
                history_lines.append(f"{role}: {body[:400]}")
        history = "\n".join(history_lines) if history_lines else "（无）"
        data, status = llm_json_object(
            system=(
                "你是 MolMind 的 SCP 会话动作分类器。只返回 JSON："
                '{"scp_dialog_act":"reuse|repeat|cache_report|execute",'
                '"report_cache":false,"reason":"..."}。'
                "按语义判定，不要把表面用词当口令白名单。"
                "reuse=基于刚才/上一轮已校验的 SCP 证据做汇总，不重新调远程工具；"
                "repeat=用上一轮科学问题重新执行查询（可再调工具）；"
                "cache_report=正常执行且主要是要审计缓存命中/实时来源；"
                "execute=普通 SCP 科研查询或执行。"
                "report_cache=true 可与 repeat/execute 并存：回复需说明缓存命中与实时来源。"
                "不要判定是否允许联网；联网授权由系统协议字段处理。"
            ),
            user=(
                f"最近对话：\n{history}\n"
                f"用户本轮原文：{text}\n"
                "请判定 scp_dialog_act 与 report_cache。"
            ),
            default={
                "scp_dialog_act": offline_act,
                "report_cache": self._scp_cache_report_requested(text),
                "reason": "offline_structural_fallback",
            },
            purpose="agent_chat",
            max_tokens=160,
            timeout_sec=8.0,
        )
        acts = {"reuse", "repeat", "cache_report", "execute"}
        act = str(data.get("scp_dialog_act") or offline_act).strip().lower()
        if act not in acts:
            act = offline_act
        report_cache = bool(data.get("report_cache"))
        if act == "cache_report":
            report_cache = True
        reason = str(data.get("reason") or status or "llm").strip() or "llm"
        if status != "ok":
            # LLM-down: trust offline regex act + cache flag.
            act = offline_act
            report_cache = self._scp_cache_report_requested(text)
            reason = f"{status};{reason}"
        return act, {
            "reason": reason,
            "status": status,
            "offline_act": offline_act,
            "report_cache": report_cache,
        }

    def _scp_previous_scientific_question(
        self, session: AgentSession
    ) -> str:
        meta = re.compile(r"(?:刚才|上一轮|之前|相同|缓存|重新执行|再次执行|基于.*证据)")
        for message in reversed(session.messages[:-1]):
            if message.get("role") != "user":
                continue
            value = str(message.get("text") or "").strip()
            if value and not meta.search(value):
                return value
        return ""

    def _scp_history_summary_reply(self, session: AgentSession) -> str:
        previous = next(
            (
                str(message.get("text") or "").strip()
                for message in reversed(session.messages[:-1])
                if message.get("role") == "assistant"
                and str(message.get("text") or "").strip()
            ),
            "",
        )
        insufficient = any(
            marker in previous
            for marker in (
                "未通过相关性校验",
                "没有获得",
                "证据不足",
                "无法据此",
                "未达到可综合回答",
            )
        )
        if insufficient:
            return (
                "基于上一轮已经校验的 Observation：\n\n"
                "1. **直接机制**：尚未检索到满足全部问题约束的直接证据。\n"
                "2. **间接机制**：放宽查询仍未形成可安全归因的完整机制链。\n"
                "3. **文献支持**：补证结果未通过相关性或排除主题校验。\n"
                "4. **证据缺口**：仍缺少同时覆盖目标疾病、组织、靶点和通路的直接资料。\n\n"
                "本轮复用了上一轮会话证据，没有重新调用远程工具。"
            )
        return (
            "以下内容复用上一轮已经生成的回答，没有重新调用远程工具：\n\n"
            + (previous or "上一轮没有可复用的科研 Observation。")
        )

    def _scp_cache_summary(self, results: list[dict[str, Any]]) -> str:
        calls = [
            call
            for result in results
            for call in result.get("calls") or []
            if call.get("tool_id")
        ]
        if not calls:
            return "本轮没有产生可审计的 SCP 调用。"
        lines = []
        for call in calls:
            status = str(call.get("cache_status") or "unknown")
            label = "缓存命中" if status == "cache_hit" else "实时查询" if status == "live" else "状态未知"
            lines.append(f"- `{call['tool_id']}`：{label}（`{status}`）")
        return "本轮缓存审计：\n\n" + "\n".join(lines)

    def _run_scp_route(
        self,
        session: AgentSession,
        *,
        route: Any,
        question: str,
        enabled_skill_ids: set[str],
        allow_cross_skill_fallback: bool = True,
    ) -> Iterator[dict[str, Any]]:
        """Execute one routed SCP capability without closing the user turn."""
        claim_scopes = self.task_router.claim_scopes(route.capability_id)
        yield self._emit(
            session,
            {"type": "thinking", "text": f"正在通过 SCP Hub 执行{route.label}。"},
        )
        events: list[dict[str, Any]] = []
        for event in self._execute_tool_adapter(
            session,
            route.tool_id,
            route.arguments,
            event_context={
                "capability_id": route.capability_id,
                "evidence_role": "primary_evidence",
                "claim_scopes": claim_scopes,
            },
        ):
            events.append(event)
            yield event
        end = next((event for event in reversed(events) if event.get("type") == "tool_end"), {})
        digest: dict[str, Any] = {}
        values: list[str] = []
        if end.get("status") == "queued":
            job_id = str(end.get("job_id") or "")
            deadline = time.monotonic() + 600.0
            job = self.scp_jobs.get(job_id, session_id=session.session_id)
            run_cancellation = self._run_controller(session).cancel_event
            while job and job.get("status") in {"queued", "running"} and time.monotonic() < deadline:
                if run_cancellation.is_set():
                    self.scp_jobs.cancel(job_id, session_id=session.session_id, reason="user_guidance")
                    raise RunInterrupted("user_guidance")
                wait_interruptible(run_cancellation, timeout_sec=1.0, slice_sec=0.25)
                job = self.scp_jobs.get(job_id, session_id=session.session_id)
            if not job or job.get("status") != "completed" or not isinstance(job.get("result"), dict):
                return {
                    "route": route,
                    "ok": False,
                    "relevant": False,
                    "values": [],
                    "digest": {},
                    "calls": [{"tool_id": route.tool_id, "cache_status": "unknown"}],
                    "error": (job or {}).get("error_code") or "job_incomplete",
                }
            result = job["result"]
            values = [
                str(block.get("value") or "")
                for block in result.get("content") or []
                if isinstance(block, dict) and str(block.get("value") or "").strip()
            ]
            digest = {
                "source": "scp-hub",
                "server_id": result.get("server_id") or "",
                "tool_name": result.get("tool_name") or "",
                "skill_id": result.get("skill_id") or route.skill_id,
                "status": result.get("status") or "hit",
                "cache_status": result.get("cache_status") or "unknown",
                "response_hash": result.get("response_hash") or "",
                "content": values,
                "writes_selection": False,
                "participates_in_ranking": False,
            }
            yield self._emit(
                session,
                {
                    "type": "job_end",
                    "job_id": job_id,
                    "tool": route.tool_id,
                    "ok": True,
                    "source": "scp-hub",
                    "status": digest["status"],
                    "cache_status": digest["cache_status"],
                    "response_hash": digest["response_hash"],
                    "digest": digest,
                },
            )
        elif end.get("ok"):
            digest = (end.get("observation") or {}).get("digest") or end.get("digest") or {}
            values = [str(value) for value in digest.get("content", []) if str(value).strip()]
        else:
            return {
                "route": route,
                "ok": False,
                "relevant": False,
                "values": [],
                "digest": {},
                "calls": [{"tool_id": route.tool_id, "cache_status": "unknown"}],
                "error": end.get("error_code") or end.get("error") or "tool_failed",
            }

        assessment = self.observation_validator.validate(
            plugin_id="scp-hub",
            capability_id=route.capability_id,
            question=question,
            values=values,
        )
        protocol_check = (
            self.observation_validator.validate_protocol(
                plugin_id="scp-hub",
                capability_id=route.capability_id,
                question=question,
                values=values,
            )
            if route.capability_id == "validation_protocol"
            else None
        )
        digest["relevance"] = assessment.as_dict()
        digest["claim_scopes"] = claim_scopes
        if protocol_check is not None:
            digest["protocol_validation"] = protocol_check
        digest["degraded_channels"] = list(
            dict.fromkeys(
                [
                    *assessment.degraded_channels,
                    *((protocol_check or {}).get("degraded_channels") or []),
                ]
            )
        )
        yield self._emit(
            session,
            {
                "type": "observation_validation",
                "source": "scp-hub",
                **assessment.as_dict(),
                "degraded_channels": digest["degraded_channels"],
                "protocol_validation": protocol_check,
                "claim_scopes": claim_scopes,
            },
        )
        if route.capability_id == "mechanism_relation_search" and not assessment.relevant:
            recovered = yield from self._recover_scp_observation(
                session,
                question=question,
                capability_id=route.capability_id,
                enabled_skill_ids=enabled_skill_ids,
                include_fallback=allow_cross_skill_fallback,
                initial_digest=digest,
            )
            if recovered is not None:
                values = recovered["values"]
                digest = recovered["digest"]
                assessment = recovered["assessment"]
                digest["claim_scopes"] = claim_scopes
        calls = [
            {
                "tool_id": route.tool_id,
                "cache_status": str((digest.get("recovery_primary") or {}).get("cache_status") or digest.get("cache_status") or "unknown"),
            }
        ]
        for item in (digest.get("recovery") or {}).get("trace") or []:
            if item.get("tool_id"):
                calls.append(
                    {
                        "tool_id": item.get("tool_id"),
                        "cache_status": item.get("cache_status") or "unknown",
                    }
                )
        relevant = bool(assessment.relevant) and (
            protocol_check is None or bool(protocol_check.get("complete"))
        )
        return {
            "route": route,
            "ok": True,
            "relevant": relevant,
            "values": values,
            "digest": digest,
            "assessment": assessment,
            "protocol_validation": protocol_check,
            "claim_scopes": claim_scopes,
            "calls": calls,
        }

    def _synthesize_scp_multi_reply(
        self, *, question: str, results: list[dict[str, Any]]
    ) -> str:
        relevant = [result for result in results if result.get("relevant")]
        if not relevant:
            subquestions = re.findall(
                r"(?:^|\n)\s*(\d+)[.、]\s*([^\n]+)", str(question or "")
            )
            if subquestions:
                protocol_selected = any(
                    result.get("route")
                    and result["route"].capability_id == "validation_protocol"
                    for result in results
                )
                lines = [
                    f"{number}. **{body.strip()}**\n   当前没有通过相关性与来源权限校验的直接证据，不能据此下结论。"
                    for number, body in subquestions
                ]
                return (
                    "本轮已执行所需的多个 SCP Capability，但上游证据未达到可综合回答的标准：\n\n"
                    + "\n\n".join(lines)
                    + (
                        "\n\n实验方案任务依赖这些证据，已安全跳过。"
                        if protocol_selected
                        else ""
                    )
                    + "\n\n实时资料不参与候选排序。"
                )
            return (
                "本轮已执行多个 SCP Capability，但没有 Observation 同时通过相关性和来源权限校验，"
                "因此不能生成跨能力科学结论。实时资料不参与候选排序。"
            )
        evidence_blocks = []
        for result in relevant:
            route = result["route"]
            evidence_blocks.append(
                {
                    "capability_id": route.capability_id,
                    "label": route.label,
                    "claim_scopes": result.get("claim_scopes") or [],
                    "response_hash": (result.get("digest") or {}).get("response_hash") or "",
                    "observation": "\n".join(result.get("values") or [])[:9000],
                }
            )
        try:
            from plugins.molmind_core.scientific.mechanism.llm_client import (
                chat_completion,
                resolve_llm_settings,
            )

            settings = resolve_llm_settings(
                {"enabled": True, "agent_chat": True}, purpose="agent_chat"
            )
            if settings.ready:
                system = (
                    "你是 MolMind 的多能力科研证据综合器。只依据输入的 Observation 回答，"
                    "并逐项回应用户编号问题。每个证据块只能支持 claim_scopes 声明的结论："
                    "mechanism_evidence 可支持机制关系；literature_evidence 可支持论文与研究发现；"
                    "experimental_design_advice 只能作为实验设计草案，绝不能用于证明论文、年份、"
                    "作者或机制事实。不得虚构 Citation、模型、药物、剂量或结论。"
                    "若某项缺少对应权限证据，明确写证据不足。区分直接证据、间接证据和设计建议。"
                    "末尾说明实时资料不参与候选排名。"
                )
                user = (
                    f"用户问题：{question}\n\n"
                    f"已验证证据块：{json.dumps(evidence_blocks, ensure_ascii=False)}"
                )
                reply = chat_completion(settings, system=system, user=user).strip()
                if reply:
                    return reply
        except Exception:
            pass
        summaries = [
            f"- {block['label']}（{', '.join(block['claim_scopes'])}）："
            f"已通过校验，response_hash={block['response_hash']}"
            for block in evidence_blocks
        ]
        return (
            "已完成多能力检索，但当前无法调用受约束综合模型；以下仅列出通过校验的证据通道：\n\n"
            + "\n".join(summaries)
            + "\n\n实时资料不参与候选排序。"
        )

    def _run_scp_multi_routes(
        self,
        session: AgentSession,
        *,
        original_question: str,
        evidence_question: str,
        routes: list[Any],
        enabled_skill_ids: set[str],
        report_cache: bool,
    ) -> Iterator[dict[str, Any]]:
        selected_ids = {route.capability_id for route in routes}
        steps = []
        for index, route in enumerate(routes, start=1):
            dependencies = [
                value
                for value in self.task_router.evidence_dependencies(route.capability_id)
                if value in selected_ids
            ]
            steps.append(
                {
                    "task_id": f"scp-{index}",
                    "tool": route.tool_id,
                    "args": route.arguments,
                    "label": route.label,
                    "capability_id": route.capability_id,
                    "depends_on": [
                        f"scp-{next(i for i, item in enumerate(routes, start=1) if item.capability_id == dep)}"
                        for dep in dependencies
                    ],
                }
            )
        yield self._emit(
            session,
            {
                "type": "agent_plan",
                "goal": original_question,
                "action": "execute",
                "diagnostics": ["task_router_multi", *[route.capability_id for route in routes]],
                "steps": steps,
            },
        )
        yield self._emit(
            session,
            {
                "type": "thinking",
                "text": "识别到多个明确科研子任务，将按证据依赖顺序逐项执行并统一综合。",
            },
        )
        results: list[dict[str, Any]] = []
        by_capability: dict[str, dict[str, Any]] = {}
        has_explicit_literature = "literature_search" in selected_ids
        for index, route in enumerate(routes, start=1):
            task_id = f"scp-{index}"
            dependencies = [
                value
                for value in self.task_router.evidence_dependencies(route.capability_id)
                if value in selected_ids
            ]
            failed_dependencies = [
                value
                for value in dependencies
                if not (by_capability.get(value) or {}).get("relevant")
            ]
            yield self._emit(session, {"type": "task_start", "task_id": task_id})
            if failed_dependencies:
                result = {
                    "route": route,
                    "ok": False,
                    "relevant": False,
                    "values": [],
                    "digest": {"claim_scopes": self.task_router.claim_scopes(route.capability_id)},
                    "claim_scopes": self.task_router.claim_scopes(route.capability_id),
                    "calls": [],
                    "error": "upstream_evidence_not_validated",
                }
                yield self._emit(
                    session,
                    {
                        "type": "task_end",
                        "task_id": task_id,
                        "status": "skipped",
                        "observation": {
                            "reason": "upstream_evidence_not_validated",
                            "dependencies": failed_dependencies,
                        },
                    },
                )
            else:
                result = yield from self._run_scp_route(
                    session,
                    route=route,
                    question=evidence_question,
                    enabled_skill_ids=enabled_skill_ids,
                    allow_cross_skill_fallback=not has_explicit_literature,
                )
                yield self._emit(
                    session,
                    {
                        "type": "task_end",
                        "task_id": task_id,
                        "status": "succeeded" if result.get("relevant") else "degraded",
                        "observation": {
                            "capability_id": route.capability_id,
                            "relevant": bool(result.get("relevant")),
                            "claim_scopes": result.get("claim_scopes") or [],
                            "response_hash": (result.get("digest") or {}).get("response_hash") or "",
                        },
                    },
                )
            results.append(result)
            by_capability[route.capability_id] = result
        reply = self._synthesize_scp_multi_reply(
            question=original_question,
            results=results,
        )
        if report_cache:
            reply += "\n\n" + self._scp_cache_summary(results)
        yield self._emit(session, {"type": "assistant", "text": reply})
        yield self._emit(session, {"type": "done"})
        self.store.persist(session)
        return True

    def _maybe_run_scp_chat(
        self, session: AgentSession, text: str
    ) -> Iterator[dict[str, Any]]:
        """Auto-dispatch an authorized scientific chat request to SCP Hub."""
        if frozen_ranking_mutation_requested(text):
            yield self._emit(
                session,
                {
                    "type": "assistant",
                    "text": "实时文献和知识图谱只能作为补充证据，不能直接重算或改写已经冻结的候选排名；如需新排名，请明确发起新的筛选请求。",
                },
            )
            yield self._emit(session, {"type": "done"})
            self.store.persist(session)
            return True
        original_text = str(text or "")
        scp_act, scp_act_meta = self._classify_scp_dialog_act(session, original_text)
        report_cache = bool(scp_act_meta.get("report_cache")) or scp_act == "cache_report"
        if scp_act == "reuse":
            yield self._emit(
                session,
                {
                    "type": "context_reuse",
                    "source": "previous_scp_observation",
                    "tool_calls": 0,
                },
            )
            yield self._emit(
                session,
                {"type": "assistant", "text": self._scp_history_summary_reply(session)},
            )
            yield self._emit(session, {"type": "done"})
            self.store.persist(session)
            return True
        repeated_question = (
            self._scp_previous_scientific_question(session)
            if scp_act == "repeat"
            else ""
        )
        routing_text = repeated_question or original_text
        plugin = self.registry.plugins.get("scp-hub")
        declared_skill_ids = {
            str(capability.get("skill_id") or "")
            for capability in (getattr(plugin, "capabilities", None) or [])
            if isinstance(capability, dict) and capability.get("skill_id")
        }
        declared_tasks = self.task_router.route_scp_tasks(
            routing_text, enabled_skill_ids=declared_skill_ids
        )
        declared_route = self.task_router.route_scp(
            routing_text, enabled_skill_ids=declared_skill_ids
        )
        if declared_route is None:
            preflight = self.task_router.plan_scp(
                routing_text,
                enabled_skill_ids=declared_skill_ids,
                recent_messages=session.messages,
                allow_unregistered=True,
            )
            if preflight is not None and preflight.route == "scp":
                declared_route = preflight
        explicitly_allowed = (
            not self._scp_live_disabled(original_text)
            and (self._scp_live_requested(original_text) or self._scp_plugin_default_live())
        )
        installed = getattr(session, "installed_scp_skills", {}) or {}
        enabled = {
            sid: state
            for sid, state in installed.items()
            if isinstance(state, dict) and state.get("enabled")
        }
        if declared_route is None and not enabled:
            return False
        if not explicitly_allowed:
            if enabled:
                yield self._emit(
                    session,
                    {
                        "type": "assistant",
                        "text": self._scp_live_denied_reply(original_text),
                    },
                )
                yield self._emit(session, {"type": "done"})
                self.store.persist(session)
                return True
            return False
        missing_declared_skills = [
            str(route.skill_id)
            for route in declared_tasks
            if route.skill_id and route.skill_id not in enabled
        ]
        if missing_declared_skills:
            yield from self._yield_scp_install_request(
                session,
                skill_ids=missing_declared_skills,
                retry_text=original_text,
                label="多项科研能力",
            )
            return True
        if declared_route is not None and declared_route.skill_id not in enabled:
            yield from self._yield_scp_install_request(
                session,
                skill_ids=[str(declared_route.skill_id)],
                retry_text=original_text,
                label=str(declared_route.label or ""),
                capability_id=str(declared_route.capability_id or ""),
            )
            return True
        if not enabled:
            fallback_ids = [
                str(declared_route.skill_id)
            ] if declared_route is not None and declared_route.skill_id else []
            if not fallback_ids:
                yield self._emit(
                    session,
                    {
                        "type": "assistant",
                        "text": (
                            "本轮明确要求实时资料，但当前会话没有可用的 SCP Skill。"
                            "请打开安装请求或在「工具与插件」中安装对应能力后再试。"
                        ),
                    },
                )
                yield self._emit(session, {"type": "done"})
                self.store.persist(session)
                return True
            yield from self._yield_scp_install_request(
                session,
                skill_ids=fallback_ids,
                retry_text=original_text,
                label=str(getattr(declared_route, "label", "") or ""),
                capability_id=str(getattr(declared_route, "capability_id", "") or ""),
            )
            return True

        multi_routes = self.task_router.route_scp_tasks(
            routing_text, enabled_skill_ids=set(enabled)
        )
        if repeated_question and len(multi_routes) > 1:
            explicitly_repeated = self.task_router.route_scp_tasks(
                original_text, enabled_skill_ids=set(enabled)
            )
            requested_ids = {
                route.capability_id for route in explicitly_repeated
            }
            if requested_ids:
                multi_routes = [
                    route
                    for route in multi_routes
                    if route.capability_id in requested_ids
                ]
        if len(multi_routes) > 1:
            return (
                yield from self._run_scp_multi_routes(
                    session,
                    original_question=original_text,
                    evidence_question=routing_text,
                    routes=multi_routes,
                    enabled_skill_ids=set(enabled),
                    report_cache=report_cache,
                )
            )

        route = self.task_router.plan_scp(
            routing_text,
            enabled_skill_ids=set(enabled),
            recent_messages=session.messages,
        )
        if route is None:
            if declared_route is None:
                return False
            yield from self._yield_scp_install_request(
                session,
                skill_ids=[str(declared_route.skill_id)],
                retry_text=original_text,
                label=str(declared_route.label or ""),
                capability_id=str(declared_route.capability_id or ""),
            )
            return True
        if route.route == "chat":
            return False
        if route.route in {"clarify", "deny"}:
            reason = str(getattr(route, "reason", "") or "")
            if route.route == "clarify" and reason.startswith("scp_skill_not_installed:"):
                skill_id = (
                    str(getattr(route, "skill_id", "") or "").strip()
                    or reason.split(":", 1)[-1].strip()
                )
                yield from self._yield_scp_install_request(
                    session,
                    skill_ids=[skill_id],
                    retry_text=original_text,
                    label=str(getattr(route, "label", "") or ""),
                    capability_id=str(getattr(route, "capability_id", "") or ""),
                )
                return True
            yield self._emit(
                session,
                {
                    "type": "assistant",
                    "text": (
                        self._clarify_reply_for_route(route)
                        if route.route == "clarify"
                        else self._deny_reply_for_route(route)
                    ),
                },
            )
            yield self._emit(session, {"type": "done"})
            self.store.persist(session)
            return True
        raw = routing_text
        skill_id, tool_id, args, label = (
            route.skill_id,
            route.tool_id,
            route.arguments,
            route.label,
        )
        if tool_id not in self.registry.tools:
            return False

        yield self._emit(session, {"type": "agent_plan", "goal": original_text, "action": "execute", "diagnostics": ["task_router", route.capability_id, route.planner_status, f"confidence:{route.confidence:.2f}", route.reason], "steps": [{"tool": tool_id, "args": args}]})
        yield self._emit(session, {"type": "thinking", "text": f"已获得实时资料授权，正在通过 SCP Hub 执行{label}。"})
        events: list[dict[str, Any]] = []
        for event in self._execute_tool_adapter(session, tool_id, args):
            events.append(event)
            yield event
        end = next((e for e in reversed(events) if e.get("type") == "tool_end"), {})
        if end.get("status") == "queued":
            job_id = str(end.get("job_id") or "")
            deadline = time.monotonic() + 600.0
            job = self.scp_jobs.get(job_id, session_id=session.session_id)
            last_progress = 0.0
            run_cancellation = self._run_controller(session).cancel_event
            while job and job.get("status") in {"queued", "running"} and time.monotonic() < deadline:
                if run_cancellation.is_set():
                    self.scp_jobs.cancel(job_id, session_id=session.session_id, reason="user_guidance")
                    raise RunInterrupted("user_guidance")
                now = time.monotonic()
                if now - last_progress >= 5.0:
                    yield self._emit(session, {"type": "thinking", "text": f"SCP Hub 正在生成{label}，后台任务 {job_id[:8]}… 仍在运行。"})
                    last_progress = now
                wait_interruptible(run_cancellation, timeout_sec=1.0, slice_sec=0.25)
                job = self.scp_jobs.get(job_id, session_id=session.session_id)
            if job and job.get("status") == "completed" and isinstance(job.get("result"), dict):
                result = job["result"]
                values = [
                    str(block.get("value") or "")
                    for block in result.get("content") or []
                    if isinstance(block, dict) and str(block.get("value") or "").strip()
                ]
                digest = {
                    "source": "scp-hub",
                    "server_id": result.get("server_id") or "",
                    "tool_name": result.get("tool_name") or "",
                    "skill_id": result.get("skill_id") or skill_id,
                    "status": result.get("status") or "hit",
                    "cache_status": result.get("cache_status") or "unknown",
                    "response_hash": result.get("response_hash") or "",
                    "content": values,
                    "writes_selection": False,
                    "participates_in_ranking": False,
                }
                assessment = self.observation_validator.validate(
                    plugin_id="scp-hub",
                    capability_id=route.capability_id,
                    question=raw,
                    values=values,
                )
                protocol_check = (
                    self.observation_validator.validate_protocol(
                        plugin_id="scp-hub",
                        capability_id=route.capability_id,
                        question=raw,
                        values=values,
                    )
                    if route.capability_id == "validation_protocol"
                    else None
                )
                digest["relevance"] = assessment.as_dict()
                digest["claim_scopes"] = self.task_router.claim_scopes(route.capability_id)
                if protocol_check is not None:
                    digest["protocol_validation"] = protocol_check
                degraded_channels = list(dict.fromkeys([
                    *assessment.degraded_channels,
                    *((protocol_check or {}).get("degraded_channels") or []),
                ]))
                digest["degraded_channels"] = degraded_channels
                yield self._emit(session, {"type": "observation_validation", "source": "scp-hub", **assessment.as_dict(), "degraded_channels": degraded_channels, "protocol_validation": protocol_check})
                yield self._emit(session, {"type": "job_end", "job_id": job_id, "tool": tool_id, "ok": True, "source": "scp-hub", "status": digest["status"], "cache_status": digest["cache_status"], "response_hash": digest["response_hash"], "digest": digest})
                reply = self._synthesize_scp_reply(question=raw, label=label, values=values, digest=digest)
            elif job and job.get("status") == "failed":
                yield self._emit(session, {"type": "job_end", "job_id": job_id, "tool": tool_id, "ok": False, "source": "scp-hub", "error_code": job.get("error_code") or "tool_failed"})
                reply = f"SCP Hub 的{label}后台任务失败（{job.get('error_code') or 'tool_failed'}）；这不代表研究结论为阴性。"
            else:
                reply = f"SCP Hub 的{label}任务仍在运行（job_id: {job_id}），可稍后查询任务状态。"
        elif end.get("ok"):
            observation = end.get("observation") or {}
            digest = observation.get("digest") or {}
            values = [str(value) for value in digest.get("content", []) if str(value).strip()]
            assessment = self.observation_validator.validate(
                plugin_id="scp-hub",
                capability_id=route.capability_id,
                question=raw,
                values=values,
            )
            protocol_check = (
                self.observation_validator.validate_protocol(
                    plugin_id="scp-hub",
                    capability_id=route.capability_id,
                    question=raw,
                    values=values,
                )
                if route.capability_id == "validation_protocol"
                else None
            )
            digest["relevance"] = assessment.as_dict()
            digest["claim_scopes"] = self.task_router.claim_scopes(route.capability_id)
            if protocol_check is not None:
                digest["protocol_validation"] = protocol_check
            degraded_channels = list(dict.fromkeys([
                *assessment.degraded_channels,
                *((protocol_check or {}).get("degraded_channels") or []),
            ]))
            digest["degraded_channels"] = degraded_channels
            yield self._emit(session, {"type": "observation_validation", "source": "scp-hub", **assessment.as_dict(), "degraded_channels": degraded_channels, "protocol_validation": protocol_check})
            if route.capability_id == "mechanism_relation_search" and not assessment.relevant:
                recovered = yield from self._recover_scp_observation(
                    session,
                    question=raw,
                    capability_id=route.capability_id,
                    enabled_skill_ids=set(enabled),
                )
                if recovered is not None:
                    values = recovered["values"]
                    digest = recovered["digest"]
                    assessment = recovered["assessment"]
                    label = recovered["label"]
                    digest["claim_scopes"] = self.task_router.claim_scopes(
                        route.capability_id
                    )
            reply = self._synthesize_scp_reply(
                question=raw,
                label=label,
                values=values,
                digest=digest,
            )
        else:
            reply = f"SCP Hub 的{label}调用未完成（{end.get('error_code') or end.get('error') or 'unknown_error'}）；这不代表研究结论为阴性。"
        yield self._emit(session, {"type": "assistant", "text": reply})
        yield self._emit(session, {"type": "done"})
        self.store.persist(session)
        return True

    def _resolve_mention(self, mention: MentionRef) -> dict[str, Any] | None:
        if mention.kind == "plugin":
            p = self.registry.plugins.get(mention.id)
            if not p:
                return None
            return {
                "kind": "plugin",
                "id": p.plugin_id,
                "title": p.title,
                "description": p.description or "（暂无描述）",
                "plugin_id": p.plugin_id,
                "builtin": p.builtin,
                "catalog": p.catalog,
                "tools": list(p.tools),
                "skills": list(p.skills),
            }
        if mention.kind == "skill":
            s = self.registry.skills.get(mention.id)
            if not s:
                return None
            return {
                "kind": "skill",
                "id": s.skill_id,
                "title": s.title,
                "description": s.description or "（暂无描述）",
                "plugin_id": s.plugin_id,
                "tools": list(s.tools),
            }
        if mention.kind == "tool":
            t = self.registry.tools.get(mention.id)
            if not t:
                return None
            return {
                "kind": "tool",
                "id": t.tool_id,
                "title": t.title,
                "description": t.description or "（暂无描述）",
                "plugin_id": t.plugin_id,
                "risk": t.risk,
                "writes_selection": t.writes_selection,
            }
        return None

    def _format_mention_intro(self, info: dict[str, Any], mention: MentionRef) -> str:
        kind_label = {"plugin": "插件", "skill": "技能", "tool": "工具"}.get(
            info["kind"], info["kind"]
        )
        lines = [
            f"**{info['title']}**（{kind_label} `{mention.raw}`）",
            info.get("description") or "",
        ]
        if info.get("plugin_id") and info["kind"] != "plugin":
            lines.append(f"所属插件：`{info['plugin_id']}`")
        if info.get("tools"):
            lines.append("关联工具：" + "、".join(f"`{x}`" for x in info["tools"]))
        if info.get("skills"):
            lines.append("关联技能：" + "、".join(f"`{x}`" for x in info["skills"]))
        if info.get("risk"):
            lines.append(f"风险等级：{info['risk']}")
        if info.get("writes_selection"):
            lines.append("注意：该工具可能涉及写榜（需策略门控）。")
        if info.get("catalog"):
            lines.append("类型：Catalog 可选插件（需在设置中安装后才会 enrichment）。")
        lines.append(
            "可单独试用：在输入框用 `/` 或 `@` 点选后加「试用」或「调用」；"
            "不会自动联动整条筛选流水线。"
        )
        return "\n".join(x for x in lines if x)

    def _handle_mentions(
        self,
        session: AgentSession,
        intent: Any,
        *,
        finalize: bool = True,
        compound: bool = False,
    ) -> Iterator[dict[str, Any]]:
        action = intent.mention_action
        yield self._emit(
            session,
            {
                "type": "thinking",
                "text": (
                    f"识别到点选 {', '.join(m.raw for m in intent.mentions)}，"
                    f"按「{'试用' if action == 'invoke' else '介绍'}」处理"
                    + (
                        "；普通对话子任务会并行完成，稍后统一汇总。"
                        if compound
                        else "，不联动其它步骤。"
                    )
                ),
            },
        )

        if action == "introduce":
            blocks: list[str] = []
            for mention in intent.mentions:
                info = self._resolve_mention(mention)
                if not info:
                    blocks.append(
                        f"未找到 `{mention.raw}`。可在「工具与插件」里确认 id，"
                        "或用 `/` `@` 重新点选。"
                    )
                    continue
                blocks.append(self._format_mention_intro(info, mention))
            yield self._emit(
                session,
                {"type": "assistant", "text": "\n\n".join(blocks)},
            )
            if finalize:
                yield self._emit(session, {"type": "done"})
                self.store.persist(session)
            return

        # invoke：逐个试用，互不强制联动
        ran_any = False
        notes: list[str] = []
        for mention in intent.mentions:
            info = self._resolve_mention(mention)
            if not info:
                notes.append(f"跳过未知项 `{mention.raw}`。")
                continue
            for ev in self._invoke_mention(session, mention, info, intent=intent):
                if ev.get("type") in ("tool_start", "card", "assistant"):
                    ran_any = True
                yield ev

        if notes:
            yield self._emit(session, {"type": "assistant", "text": "\n".join(notes)})
        if not ran_any and not notes:
            yield self._emit(
                session,
                {
                    "type": "assistant",
                    "text": (
                        "当前点选项暂不支持单独试用。"
                        "可先发「介绍」了解用途，或用自然语言描述完整需求让我编排技能。"
                    ),
                },
            )
        if finalize:
            yield self._emit(session, {"type": "done"})
            self.store.persist(session)

    def _invoke_mention(
        self,
        session: AgentSession,
        mention: MentionRef,
        info: dict[str, Any],
        *,
        intent: AgentIntent | None = None,
    ) -> Iterator[dict[str, Any]]:
        kind = info["kind"]
        mid = info["id"]

        if (kind == "tool" and mid == "query_evidence") or (
            kind == "skill" and mid == "masld_explain"
        ):
            yield from self._run_query_evidence(session, intent or parse_intent(mention.raw))
            return

        if (kind == "skill" and mid == "masld_export_bundle") or (
            kind == "tool" and mid == "export_submission_bundle"
        ):
            if session.last_result is None:
                yield self._emit(
                    session,
                    {
                        "type": "assistant",
                        "text": (
                            f"试用 `{mention.raw}` 需要已有冻结筛选结果。"
                            "请先完成候选筛选，再导出包含候选清单与候补名单的结果归档包。"
                        ),
                    },
                )
                return
            yield from self._execute_tool_adapter(
                session,
                "export_submission_bundle",
                {},
            )
            return

        # 技能 / 核心工具：可单独跑对应段
        if (kind == "skill" and mid == "masld_nominate") or (
            kind == "tool" and mid in ("score_and_rank", "export_nomination")
        ):
            if mid == "export_nomination":
                if session.last_result is None:
                    yield self._emit(
                        session,
                        {
                            "type": "assistant",
                            "text": (
                                "试用 `export_nomination` 需要已有筛选结果。"
                                "请先试用 `@skill:masld_nominate` 或上传 SDF 后跑筛选。"
                            ),
                        },
                    )
                    return
                yield from self._execute_tool_adapter(
                    session,
                    "export_nomination",
                    {"tier": "primary"},
                )
                return
            if not session.sdf_bytes:
                yield self._emit(
                    session,
                    {
                        "type": "assistant",
                        "text": (
                            f"试用 `{mention.raw}` 需要先上传 .sdf 附件。"
                            "上传后再说一次「试用」即可单独跑筛选排序，不会顺带生成机制 PDF。"
                        ),
                    },
                )
                return
            yield self._emit(
                session,
                {
                    "type": "plan",
                    "steps": [f"单独试用 {mention.raw}：生成 Top{session.top_n} 候选清单"],
                },
            )
            score_ok = False
            for event in self._execute_tool_adapter(
                session,
                "score_and_rank",
                {"top_n": session.top_n},
            ):
                if (
                    event.get("type") == "tool_end"
                    and event.get("tool") == "score_and_rank"
                ):
                    score_ok = bool(event.get("ok"))
                yield event
            if score_ok:
                yield from self._execute_tool_adapter(
                    session,
                    "export_nomination",
                    {"tier": "primary"},
                )
            yield self._emit(
                session,
                {
                    "type": "assistant",
                    "text": f"已单独试用 `{mention.raw}`（未联动机制 PDF / Catalog）。",
                },
            )
            return

        if (kind == "skill" and mid == "masld_mechanism") or (
            kind == "tool" and mid in ("start_mechanism_report", "get_mechanism_job")
        ):
            if session.last_result is None:
                yield self._emit(
                    session,
                    {
                        "type": "assistant",
                        "text": (
                            f"试用 `{mention.raw}` 需要已有筛选结果。"
                            "请先完成筛选或试用 `@skill:masld_nominate`。"
                        ),
                    },
                )
                return
            if mid == "get_mechanism_job":
                yield from self._execute_tool_adapter(
                    session,
                    "get_mechanism_job",
                    {},
                )
                return
            yield self._emit(
                session,
                {
                    "type": "plan",
                    "steps": [f"单独试用 {mention.raw}：生成机制 PDF"],
                },
            )
            yield from self._execute_tool_adapter(
                session,
                "start_mechanism_report",
                {},
            )
            yield self._emit(
                session,
                {
                    "type": "assistant",
                    "text": f"已单独试用 `{mention.raw}`（未重新跑筛选）。",
                },
            )
            return

        # Catalog tool 单独试用
        if kind == "tool" and mid in TOOL_HANDLERS:
            if mid.startswith("mcp_") or mid == "predict_pl_fitness":
                parent = info.get("plugin_id")
                if parent and parent not in session.installed_catalog:
                    yield self._emit(
                        session,
                        {
                            "type": "assistant",
                            "text": (
                                f"工具 `{mid}` 属于 Catalog 插件 `{parent}`，"
                                "请先在「工具与插件」中安装后再试用。"
                            ),
                        },
                    )
                    return
            kwargs: dict[str, Any] = {}
            if mid.startswith("mcp_"):
                kwargs["query"] = "trial"
            elif mid == "predict_pl_fitness":
                kwargs["smiles_list"] = []
            decision = self._authorize_tool_call(session, mid, kwargs)
            if not decision.allowed:
                yield from self._governance_denied_events(
                    session,
                    tool_id=mid,
                    decision=decision,
                )
                return
            yield self._emit(
                session,
                {
                    "type": "tool_start",
                    "tool": mid,
                    "plugin": info.get("plugin_id") or "",
                    "args": {
                        **kwargs,
                        "trial": True,
                        "writes_selection": False,
                    },
                },
            )
            try:
                result = dispatch_tool(mid, **kwargs)
                yield self._emit(
                    session,
                    {
                        "type": "tool_end",
                        "tool": mid,
                        "ok": bool(result.get("ok", True)),
                        "digest": result.get("digest") or {},
                    },
                )
                yield self._emit(
                    session,
                    {
                        "type": "assistant",
                        "text": (
                            result.get("message")
                            or f"已单独试用 Catalog 工具 `{mid}`（不改排名）。"
                        ),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                yield self._emit(
                    session,
                    {"type": "tool_end", "tool": mid, "ok": False, "error": str(exc)},
                )
                yield self._emit(
                    session,
                    {"type": "assistant", "text": f"试用 `{mid}` 失败：{exc}"},
                )
            return

        # Catalog 插件：仅跑该插件 enrichment
        if kind == "plugin" and info.get("catalog"):
            if mid not in session.installed_catalog:
                yield self._emit(
                    session,
                    {
                        "type": "assistant",
                        "text": (
                            f"插件 `{mid}` 尚未安装。请到「工具与插件」添加后再试用 enrichment。"
                        ),
                    },
                )
                return
            if session.last_result is None:
                yield self._emit(
                    session,
                    {
                        "type": "assistant",
                        "text": (
                            f"试用 Catalog 插件 `{mid}` 建议先有筛选结果作旁证输入。"
                            "也可先「介绍」了解能力。"
                        ),
                    },
                )
                return
            yield from self._run_catalog_enrichment(session, only_plugin=mid)
            yield self._emit(
                session,
                {
                    "type": "assistant",
                    "text": f"已单独试用 Catalog 插件 `{mid}` enrichment（不改主榜）。",
                },
            )
            return

        # 其余工具：不可单独执行，给出介绍
        yield self._emit(
            session,
            {
                "type": "assistant",
                "text": (
                    self._format_mention_intro(info, mention)
                    + "\n\n当前该项不支持独立「试用」执行（可能只是流水线中间步骤）。"
                    "若要产物，请用自然语言说明目标（如生成 top10 csv）。"
                ),
            },
        )

    def _run_query_evidence(
        self,
        session: AgentSession,
        intent: AgentIntent,
    ) -> Iterator[dict[str, Any]]:
        """Run the canonical local-first evidence Tool without touching selection."""
        molecule_id = intent.evidence_molecule_id
        rank_reference = re.fullmatch(
            r"top[\s\-_]*(\d{1,3})",
            str(molecule_id or "").strip(),
            re.I,
        )
        if rank_reference:
            requested_rank = int(rank_reference.group(1))
            frozen_top = list(
                getattr(session.last_result, "top_molecules", None) or []
            )
            if 1 <= requested_rank <= len(frozen_top):
                molecule_id = str(frozen_top[requested_rank - 1].molecule_id)
            else:
                reply = (
                    f"当前冻结主榜只有 Top {len(frozen_top)}，无法定位 Top {requested_rank} "
                    "对应的分子，因此没有执行证据查询。"
                    if frozen_top
                    else "当前没有可用于解析 Top1 的冻结主榜；请提供具体 molecule_id。"
                )
                yield self._emit(
                    session,
                    {
                        "type": "tool_end",
                        "tool": "query_evidence",
                        "ok": False,
                        "error": "ranking_reference_unresolved",
                        "digest": {
                            "requested_rank": requested_rank,
                            "frozen_primary_count": len(frozen_top),
                            "writes_selection": False,
                        },
                    },
                )
                yield self._emit(session, {"type": "assistant", "text": reply})
                return
        inchikey = intent.evidence_inchikey
        cas = intent.evidence_cas
        smiles = intent.evidence_smiles
        providers = list(intent.evidence_providers) or None
        query_types = list(intent.evidence_query_types) or None
        allow_live = bool(intent.evidence_allow_live)
        force_refresh = bool(intent.evidence_force_refresh)
        total_timeout_sec = float(intent.evidence_total_timeout_sec or 45.0)
        display_providers = list(providers or [])
        if providers is None:
            try:
                from plugins.molmind_core.scientific.evidence_gateway.retriever import (
                    load_provider_config,
                )

                provider_policy = load_provider_config()
                configured = provider_policy.get("providers") or {}
                display_providers = [
                    str(provider_id)
                    for provider_id, provider_cfg in configured.items()
                    if isinstance(provider_cfg, dict)
                    and provider_cfg.get("enabled", True)
                    and provider_cfg.get(
                        "query_tool_default",
                        bool(provider_cfg.get("identity_order")),
                    )
                ]
            except (OSError, ValueError, TypeError):
                # The canonical handler remains authoritative and will return
                # a structured configuration failure if policy loading fails.
                display_providers = []
        tool_timeout = self.registry.tools["query_evidence"].timeout_sec or 45.0
        total_timeout_sec = min(total_timeout_sec, float(tool_timeout))
        governance_args = {
            key: value
            for key, value in {
                "molecule_id": molecule_id,
                "inchikey": inchikey,
                "cas": cas,
                "smiles": smiles,
                "providers": list(providers) if providers is not None else None,
                "query_types": query_types,
                "allow_live": allow_live,
                "force_refresh": force_refresh,
                "total_timeout_sec": total_timeout_sec,
            }.items()
            if value is not None
        }
        decision = self._authorize_tool_call(
            session,
            "query_evidence",
            governance_args,
            confirmed_scopes={"allow_live"} if allow_live else set(),
        )
        if not decision.allowed:
            yield from self._governance_denied_events(
                session,
                tool_id="query_evidence",
                decision=decision,
            )
            yield self._emit(
                session,
                {
                    "type": "assistant",
                    "text": (
                        "证据查询未执行："
                        f"{decision.message}。这不表示候选无效、无毒或没有证据。"
                    ),
                },
            )
            return
        selection_before = str(session.last_selection_sha256 or "")
        result_selection_before = str(
            getattr(session.last_result, "selection_sha256", "") or ""
        )
        selection_snapshot, selection_digest_before = _selection_guard_snapshot(
            session.last_result
        )

        requested_identity = {
            key: value
            for key, value in {
                "molecule_id": molecule_id,
                "inchikey": inchikey,
                "cas": cas,
                "smiles": smiles,
            }.items()
            if value
        }
        yield self._emit(
            session,
            {
                "type": "query_plan",
                "molecule_id": molecule_id,
                "identity": requested_identity,
                "providers": display_providers,
                "query_types": query_types or [],
                "allow_live": allow_live,
                "force_refresh": force_refresh,
                "deadline": total_timeout_sec,
                "local_sources": [
                    "frozen_snapshot",
                    "local_public_qc",
                    "dilirank_gate",
                    "epa_ctx_frozen_stage",
                ],
                "cached_remote_sources": [],
                "remote_provider_plan": display_providers if allow_live else [],
                "skipped_or_unsupported_sources": [] if allow_live else display_providers,
                "message": (
                    "冻结快照 → 本地公开数据/QC → 查询状态缓存"
                    + (" → 显式 live provider" if allow_live else " → 不访问远端")
                    + " → 规范化证据卡"
                ),
            },
        )
        yield self._emit(
            session,
            {
                "type": "tool_start",
                "tool": "query_evidence",
                "plugin": "molmind-core",
                "args": {
                    "molecule_id": molecule_id,
                    "identity_fields": sorted(requested_identity),
                    "providers": display_providers,
                    "provider_selection": "explicit" if providers is not None else "default",
                    "query_types": query_types,
                    "allow_live": allow_live,
                    "force_refresh": force_refresh,
                    "total_timeout_sec": total_timeout_sec,
                    "writes_selection": False,
                },
            },
        )

        event_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        saw_query_summary = False
        accepting_events = threading.Event()
        accepting_events.set()
        cancellation = threading.Event()
        run_cancellation = self._run_controller(session).cancel_event

        def on_query_event(event: object) -> None:
            if not accepting_events.is_set():
                return
            safe = _sanitize_query_event(event)
            if safe is not None:
                event_queue.put(("event", safe))

        def invoke_handler() -> None:
            try:
                # Lazy import keeps the Agent shell usable while optional provider
                # dependencies initialize, and exposes one canonical plugin handler.
                from plugins.molmind_core.tools.scientific import run_query_evidence

                query_result = run_query_evidence(
                    # Compatibility view contains no mutable ranking objects.
                    result=_ReadOnlyResultSnapshot(result_selection_before),
                    molecule_index=session.last_molecule_index or None,
                    molecule_id=molecule_id,
                    inchikey=inchikey,
                    cas=cas,
                    smiles=smiles,
                    providers=providers,
                    query_types=query_types,
                    allow_live=allow_live,
                    force_refresh=force_refresh,
                    total_timeout_sec=total_timeout_sec,
                    cancel_event=cancellation,
                    event_sink=on_query_event,
                )
                event_queue.put(("result", query_result))
            except Exception as exc:  # noqa: BLE001 — handed to sanitized parent path
                event_queue.put(("error", exc))
            finally:
                event_queue.put(("done", None))

        started = time.perf_counter()
        worker = threading.Thread(
            target=invoke_handler,
            name="query-evidence",
            daemon=True,
        )
        worker.start()
        query_result: Any | None = None
        query_error: Exception | None = None
        query_deadline = started + total_timeout_sec
        while True:
            if run_cancellation.is_set():
                accepting_events.clear()
                cancellation.set()
                raise RunInterrupted("user_guidance")
            if time.perf_counter() >= query_deadline:
                accepting_events.clear()
                cancellation.set()
                query_error = TimeoutError("query_evidence total deadline exceeded")
                break
            try:
                item_type, payload = event_queue.get(timeout=0.25)
            except queue.Empty:
                # Bounded blocking wait: no high-frequency provider polling.
                continue
            if item_type == "event":
                if payload.get("type") == "query_summary":
                    saw_query_summary = True
                try:
                    yield self._emit(session, payload)
                except GeneratorExit:
                    # Client disconnect: stop accepting queue events and ask
                    # the gateway to cancel unsent work before closing.
                    accepting_events.clear()
                    cancellation.set()
                    worker.join(timeout=0.1)
                    raise
            elif item_type == "result":
                query_result = payload
            elif item_type == "error" and isinstance(payload, Exception):
                query_error = payload
            elif item_type == "done":
                break
        accepting_events.clear()
        if query_error is not None:
            cancellation.set()
        worker.join(timeout=0.1)

        if query_error is not None or query_result is None:
            exc = query_error or RuntimeError("query handler returned no result")
            elapsed = time.perf_counter() - started
            selection_after = str(session.last_selection_sha256 or "")
            result_selection_after = str(
                getattr(session.last_result, "selection_sha256", "") or ""
            )
            _, selection_digest_after = _selection_guard_snapshot(session.last_result)
            selection_unchanged = (
                selection_after == selection_before
                and result_selection_after == result_selection_before
                and selection_digest_after == selection_digest_before
            )
            if not selection_unchanged:
                _restore_selection_guard(session.last_result, selection_snapshot)
                session.last_selection_sha256 = selection_before
                if session.last_result is not None:
                    try:
                        session.last_result.selection_sha256 = result_selection_before
                    except (AttributeError, TypeError):
                        pass
            elif session.last_result is not None and not hasattr(
                session.last_result, "selection_sha256"
            ):
                try:
                    session.last_result.selection_sha256 = result_selection_before
                except (AttributeError, TypeError):
                    pass
            error_code = (
                "selection_mutation_blocked"
                if isinstance(exc, _SelectionMutationAttempt)
                else (
                    "query_failed"
                    if isinstance(exc, TimeoutError)
                    else "query_handler_failed"
                )
            )
            yield self._emit(
                session,
                {
                    "type": "query_summary",
                    "status": error_code,
                    "allow_live": allow_live,
                    "degraded_channels": [error_code],
                    "selection_sha256_unchanged": selection_unchanged,
                    "message": (
                        "证据查询未完成；已记录为 query_failed，不能据此推断无效或无毒。"
                    ),
                },
            )
            yield self._emit(
                session,
                {
                    "type": "tool_end",
                    "tool": "query_evidence",
                    "ok": False,
                    "elapsed_s": round(elapsed, 3),
                    "error": error_code,
                    "digest": {
                        "error_code": error_code,
                        "exception_type": type(exc).__name__,
                        "writes_selection": False,
                        "selection_sha256_unchanged": selection_unchanged,
                    },
                },
            )
            if error_code == "selection_mutation_blocked":
                yield self._emit(
                    session,
                    {
                        "type": "card",
                        "card": {
                            "kind": "evidence",
                            "title": "候选分子证据卡",
                            "status": error_code,
                            "summary": "检测到只读证据工具试图改变主榜状态，已阻断。",
                            "writes_selection": False,
                            "selection_sha256_unchanged": selection_unchanged,
                        },
                    },
                )
            reply = (
                "证据查询未完成（query_failed）。这表示查询通道失败，不代表该候选无效、"
                "无毒或没有证据；主榜未被修改。"
            )
            yield self._emit(session, {"type": "assistant", "text": reply})
            return

        elapsed = time.perf_counter() - started

        card_raw = getattr(query_result, "card", None)
        card_status = (
            str(card_raw.get("status") or "").strip()
            if isinstance(card_raw, dict)
            else ""
        )
        ok = bool(getattr(query_result, "ok", False))
        error_code = str(getattr(query_result, "error_code", "") or "")
        status = card_status or ("hit" if ok else (error_code or "query_failed"))
        degraded_channels = [
            str(item)
            for item in (getattr(query_result, "degraded_channels", None) or [])
            if str(item)
        ]
        identity_raw = getattr(query_result, "identity", None)
        identity = _safe_query_value(identity_raw) if identity_raw else requested_identity
        message = _redact_query_text(
            getattr(query_result, "message", None)
            or (
                "证据查询完成；结果只作证据与解释，不修改主榜。"
                if ok
                else "证据查询没有得到可用结果；不能据此推断阴性、无毒或无效。"
            )
        )

        selection_after = str(session.last_selection_sha256 or "")
        result_selection_after = str(
            getattr(session.last_result, "selection_sha256", "") or ""
        )
        _, selection_digest_after = _selection_guard_snapshot(session.last_result)
        selection_unchanged = (
            selection_after == selection_before
            and result_selection_after == result_selection_before
            and selection_digest_after == selection_digest_before
        )
        if not selection_unchanged:
            # Defense in depth: query_evidence is R0. Restore the persisted hash
            # and surface the invariant breach instead of reporting success.
            _restore_selection_guard(session.last_result, selection_snapshot)
            session.last_selection_sha256 = selection_before
            if session.last_result is not None:
                try:
                    session.last_result.selection_sha256 = result_selection_before
                except (AttributeError, TypeError):
                    pass
            ok = False
            status = "selection_mutation_blocked"
            error_code = status
            if status not in degraded_channels:
                degraded_channels.append(status)
            message = "检测到只读证据工具试图改变主榜状态，已阻断并恢复选择哈希。"

        if not saw_query_summary or status == "selection_mutation_blocked":
            yield self._emit(
                session,
                {
                    "type": "query_summary",
                    "status": status,
                    "molecule_id": molecule_id,
                    "identity": identity,
                    "allow_live": allow_live,
                    "degraded_channels": degraded_channels,
                    "selection_sha256_unchanged": selection_unchanged,
                    "message": message,
                },
            )

        yield self._emit(
            session,
            {
                "type": "tool_end",
                "tool": "query_evidence",
                "ok": ok,
                "elapsed_s": round(elapsed, 3),
                "error": error_code if not ok else "",
                "digest": {
                    "status": status,
                    "error_code": error_code or None,
                    "degraded_channels": degraded_channels,
                    "allow_live": allow_live,
                    "writes_selection": False,
                    "selection_sha256_before": selection_before,
                    "selection_sha256_after": selection_after,
                    "selection_sha256_unchanged": selection_unchanged,
                },
            },
        )

        card = _safe_query_value(card_raw) if isinstance(card_raw, dict) else {}
        if not isinstance(card, dict):
            card = {}
        card_payload = {
            **card,
            "kind": "evidence",
            "title": card.get("title") or "候选分子证据卡",
            "status": (
                status
                if status == "selection_mutation_blocked"
                else (card.get("status") or status)
            ),
            "identity": card.get("identity") or identity or requested_identity,
            "summary": (
                message
                if status == "selection_mutation_blocked"
                else (card.get("summary") or message)
            ),
            "allow_live": allow_live,
            "degraded_channels": card.get("degraded_channels") or degraded_channels,
            "writes_selection": False,
            "selection_sha256_unchanged": selection_unchanged,
        }
        yield self._emit(
            session,
            {
                "type": "card",
                "card": card_payload,
            },
        )
        yield self._emit(session, {"type": "assistant", "text": message})

    @staticmethod
    def _artifact_download_url(session: AgentSession, artifact: Artifact) -> str:
        return (
            f"/api/agent/sessions/{session.session_id}/artifacts/"
            f"{artifact.artifact_id}/download"
        )

    @staticmethod
    def _submission_csv_name(
        session: AgentSession, *, tier: str, primary_count: int | None = None
    ) -> str:
        source = Path(session.sdf_filename or "library.sdf").stem or "library"
        if tier == "primary":
            count = max(1, int(primary_count or 1))
            suffix = f"nomination_top{count}.csv"
        else:
            suffix = "nomination_reserve.csv"
        return f"{source}_{suffix}"

    def _csv_artifact(self, session: AgentSession, *, tier: str, trial: bool = False) -> Artifact:
        result = session.last_result
        if result is None:
            raise RuntimeError("尚无冻结筛选结果")
        if tier == "primary":
            csv_text = result.to_csv_text()
            title = f"候选分子清单：Top {len(result.top_molecules)}"
            selection_hash = result.selection_sha256
        elif tier == "reserve":
            csv_text = result.to_reserve_csv_text()
            title = "候补名单：冻结顺延顺序"
            selection_hash = result.reserve_selection_sha256
        else:
            raise ValueError(f"未知导出层级: {tier}")
        subtitle = (
            f"run_id={result.run_id[:12]}… · selection_sha256={selection_hash[:12]}…"
        )
        if trial:
            subtitle = "单独试用 export_nomination · " + subtitle
        return Artifact(
            artifact_id=_aid(),
            kind="csv",
            filename=self._submission_csv_name(
                session,
                tier=tier,
                primary_count=(len(result.top_molecules) if tier == "primary" else None),
            ),
            title=title,
            subtitle=subtitle,
            media_type="text/csv; charset=utf-8",
            # Browser artifact downloads are CSV deliverables, so include BOM
            # just like on-disk exports for direct Excel opening.
            content=("\ufeff" + csv_text).encode("utf-8"),
        )

    def _emit_artifact_card(
        self, session: AgentSession, artifact: Artifact
    ) -> Iterator[dict[str, Any]]:
        yield self._emit(
            session,
            {
                "type": "card",
                "card": {
                    "kind": artifact.kind,
                    "title": artifact.title,
                    "subtitle": artifact.subtitle,
                    "filename": artifact.filename,
                    "artifact_id": artifact.artifact_id,
                    "download_url": self._artifact_download_url(session, artifact),
                },
            },
        )

    def _export_reserve_only(self, session: AgentSession) -> Iterator[dict[str, Any]]:
        ensure_session_last_result(session)
        result = session.last_result
        if result is None:
            yield self._emit(
                session,
                {
                    "type": "tool_start",
                    "tool": "export_nomination",
                    "plugin": "molmind-core",
                    "args": {"format": "csv", "tier": "reserve"},
                },
            )
            yield self._emit(
                session,
                {
                    "type": "tool_end",
                    "tool": "export_nomination",
                    "ok": False,
                    "error_code": "missing_precondition",
                    "error": "缺少前置条件：frozen_result",
                    "status": "denied",
                },
            )
            return
        yield self._emit(
            session,
            {
                "type": "tool_start",
                "tool": "export_nomination",
                "plugin": "molmind-core",
                "args": {
                    "format": "csv",
                    "tier": "reserve",
                    "reserve_n": result.config.reserve_n,
                    "run_id": result.run_id,
                },
            },
        )
        artifact = self._csv_artifact(session, tier="reserve")
        self.store.put_artifact(session, artifact)
        digest: dict[str, Any] = {
            "artifact_id": artifact.artifact_id,
            "bytes": len(artifact.content),
            "run_id": result.run_id,
            "selection_sha256": result.reserve_selection_sha256,
            "reserve_count": len(result.reserve_molecules),
        }
        if len(result.reserve_molecules) < result.config.reserve_n:
            digest["reserve_note"] = reserve_shortage_note(
                actual_count=len(result.reserve_molecules),
                requested_count=result.config.reserve_n,
            )
        yield self._emit(
            session,
            {"type": "tool_end", "tool": "export_nomination", "ok": True, "digest": digest},
        )
        yield from self._emit_artifact_card(session, artifact)

    def _export_primary_only(self, session: AgentSession) -> Iterator[dict[str, Any]]:
        ensure_session_last_result(session)
        result = session.last_result
        if result is None:
            yield self._emit(
                session,
                {
                    "type": "tool_start",
                    "tool": "export_nomination",
                    "plugin": "molmind-core",
                    "args": {"format": "csv", "tier": "primary"},
                },
            )
            yield self._emit(
                session,
                {
                    "type": "tool_end",
                    "tool": "export_nomination",
                    "ok": False,
                    "error_code": "missing_precondition",
                    "error": "缺少前置条件：frozen_result",
                    "status": "denied",
                },
            )
            return
        expected_name = self._submission_csv_name(
            session, tier="primary", primary_count=len(result.top_molecules)
        )
        existing = next(
            (
                artifact
                for artifact in session.artifacts.values()
                if artifact.kind == "csv"
                and artifact.filename == expected_name
                and f"run_id={result.run_id[:12]}" in artifact.subtitle
            ),
            None,
        )
        yield self._emit(
            session,
            {
                "type": "tool_start",
                "tool": "export_nomination",
                "plugin": "molmind-core",
                "args": {
                    "format": "csv",
                    "tier": "primary",
                    "top_n": len(result.top_molecules),
                    "run_id": result.run_id,
                    "reuse": existing is not None,
                },
            },
        )
        if existing is not None:
            yield self._emit(
                session,
                {
                    "type": "tool_end",
                    "tool": "export_nomination",
                    "ok": True,
                    "digest": {
                        "artifact_id": existing.artifact_id,
                        "bytes": len(existing.content),
                        "run_id": result.run_id,
                        "reused": True,
                    },
                },
            )
            yield from self._emit_artifact_card(session, existing)
            return
        artifact = self._csv_artifact(session, tier="primary")
        self.store.put_artifact(session, artifact)
        yield self._emit(
            session,
            {
                "type": "tool_end",
                "tool": "export_nomination",
                "ok": True,
                "digest": {
                    "artifact_id": artifact.artifact_id,
                    "bytes": len(artifact.content),
                    "run_id": result.run_id,
                    "selection_sha256": result.selection_sha256,
                    "primary_count": len(result.top_molecules),
                },
            },
        )
        yield from self._emit_artifact_card(session, artifact)

    def _export_submission_bundle(self, session: AgentSession) -> Iterator[dict[str, Any]]:
        result = session.last_result
        if result is None:
            return
        yield self._emit(
            session,
            {
                "type": "tool_start",
                "tool": "export_submission_bundle",
                "plugin": "molmind-core",
                "args": {"run_id": result.run_id, "writes_selection": False},
            },
        )
        primary_name = self._submission_csv_name(
            session, tier="primary", primary_count=len(result.top_molecules)
        )
        reserve_name = self._submission_csv_name(session, tier="reserve")
        stem = Path(session.sdf_filename or "library.sdf").stem or "library"
        manifest = {
            "schema_version": "molmind-agent-submission-bundle-v1",
            "run_id": result.run_id,
            "input_sha256": result.input_sha256,
            "config_hash": result.config.config_hash,
            "primary": {
                "filename": primary_name,
                "count": len(result.top_molecules),
                "selection_sha256": result.selection_sha256,
                "nomination_tier": "primary",
            },
            "reserve": {
                "filename": reserve_name,
                "count": len(result.reserve_molecules),
                "requested_count": result.config.reserve_n,
                "selection_sha256": result.reserve_selection_sha256,
                "nomination_tier": "reserve",
                "promotion_rule": (
                    "仅在主榜候选不可采购、无法配制或身份复核失败时，"
                    "优先按冻结 reserve_rank 顺延；保留 replacement_for 关联。"
                ),
                "shortage_note": (
                    reserve_shortage_note(
                        actual_count=len(result.reserve_molecules),
                        requested_count=result.config.reserve_n,
                    )
                    if len(result.reserve_molecules) < result.config.reserve_n
                    else ""
                ),
            },
        }
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(primary_name, ("\ufeff" + result.to_csv_text()).encode("utf-8"))
            zf.writestr(reserve_name, ("\ufeff" + result.to_reserve_csv_text()).encode("utf-8"))
            zf.writestr(
                f"{stem}_submission_manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            trace = self.store.read_events(session.session_id)
            zf.writestr(
                f"{stem}_agent_trace.jsonl",
                ("\n".join(json.dumps(event, ensure_ascii=False) for event in trace) + "\n").encode("utf-8"),
            )
            for artifact in session.artifacts.values():
                if artifact.kind == "pdf" and result.run_id[:8] in artifact.subtitle:
                    zf.writestr(artifact.filename, artifact.content)
        artifact = Artifact(
            artifact_id=_aid(),
            kind="bundle",
            filename=f"{stem}_results_bundle.zip",
            title="结果归档包：候选清单 + 候补 + 审计",
            subtitle=(
                f"run_id={result.run_id[:12]}… · 候选={len(result.top_molecules)} · "
                f"候补={len(result.reserve_molecules)}"
            ),
            media_type="application/zip",
            content=archive.getvalue(),
        )
        self.store.put_artifact(session, artifact)
        yield self._emit(
            session,
            {
                "type": "tool_end",
                "tool": "export_submission_bundle",
                "ok": True,
                "digest": {
                    "artifact_id": artifact.artifact_id,
                    "bytes": len(artifact.content),
                    "run_id": result.run_id,
                    "primary_selection_sha256": result.selection_sha256,
                    "reserve_selection_sha256": result.reserve_selection_sha256,
                },
            },
        )
        yield from self._emit_artifact_card(session, artifact)

    def _export_nomination_only(self, session: AgentSession) -> Iterator[dict[str, Any]]:
        result = session.last_result
        if result is None:
            return
        top_n = session.top_n
        yield self._emit(
            session,
            {
                "type": "tool_start",
                "tool": "export_nomination",
                "plugin": "molmind-core",
                "args": {"format": "csv", "top_n": top_n, "trial": True},
            },
        )
        art = self._csv_artifact(session, tier="primary", trial=True)
        self.store.put_artifact(session, art)
        yield self._emit(
            session,
            {
                "type": "tool_end",
                "tool": "export_nomination",
                "ok": True,
                "digest": {"artifact_id": art.artifact_id, "bytes": len(art.content)},
            },
        )
        yield from self._emit_artifact_card(session, art)
        yield self._emit(
            session,
            {
                "type": "assistant",
                "text": format_run_completion(
                    want_csv=True,
                    want_pdf=False,
                    result=result,
                ),
            },
        )

    def _attachment_context_text(self, session: AgentSession) -> str:
        """Collect non-SDF attachment summaries for planner/chat prompts."""
        summaries: list[dict[str, Any]] = []
        active = session.active_run or {}
        for item in active.get("attachment_summaries") or []:
            if isinstance(item, dict):
                summaries.append(item)
        if not summaries:
            for message in reversed(session.messages[-4:]):
                for item in message.get("attachments") or []:
                    if not isinstance(item, dict):
                        continue
                    kind = str(item.get("kind") or "")
                    if kind == "sdf":
                        continue
                    if item.get("excerpt") or item.get("note"):
                        summaries.append(item)
                if summaries:
                    break
        return format_attachment_context(summaries)

    def _llm_chat_reply(self, session: AgentSession, text: str) -> str:
        """Answer general questions with LLM; fall back to a short template."""
        try:
            return "".join(self._llm_chat_reply_stream(session, text)).strip() or (
                self._chat_reply_fallback(session, text)
            )
        except CallCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — LLM optional
            if self._run_controller(session).interruption_requested:
                raise RunInterrupted("user_guidance") from exc
            return self._chat_reply_fallback(session, text)

    def _llm_chat_reply_stream(
        self, session: AgentSession, text: str
    ) -> Iterator[str]:
        """Yield token deltas for a conversational reply; records working memory once."""
        from plugins.molmind_core.scientific.mechanism.llm_client import (
            MechanismLLMError,
            chat_completion_stream,
            resolve_llm_settings,
        )

        settings = resolve_llm_settings(
            {"enabled": True, "agent_chat": True}, purpose="agent_chat"
        )
        if not settings.ready:
            yield self._chat_reply_fallback(session, text)
            return

        # Slightly warmer, no cache — conversational Q&A.
        settings = type(settings)(
            enabled=settings.enabled,
            model=settings.model,
            base_url=settings.base_url,
            api_key=settings.api_key,
            temperature=0.4,
            timeout_sec=max(settings.timeout_sec, 45.0),
            max_tokens=min(max(settings.max_tokens, 1024), 2048),
            cache_dir=settings.cache_dir,
            use_cache=False,
        )

        has_sdf = bool(session.sdf_bytes)
        name = session.sdf_filename or ""
        attachment_context = self._attachment_context_text(session)
        frozen_result = session.last_result
        frozen_count = len(getattr(frozen_result, "top_molecules", None) or [])
        frozen_run_id = str(getattr(frozen_result, "run_id", "") or "").strip()
        frozen_context = (
            f"本会话最近冻结结果：Top {frozen_count}"
            + (f"，run_id={frozen_run_id}" if frozen_run_id else "")
            if frozen_result is not None
            else "本会话最近冻结结果：无"
        )
        active_resume = (session.active_run or {}).get("resume_context") or {}
        context_window = build_context_window(
            messages=session.messages[:-1],
            working_memory=session.working_memory,
            resume_context=active_resume,
        )
        history = context_window.history
        working_context = context_window.working_memory
        resume_context = context_window.resume_context
        if context_window.summary != session.context_summary:
            session.context_summary = context_window.summary
            self.store.persist(session)

        from agent.runtime.capability_context import (
            build_capability_surface,
            format_capability_surface_for_prompt,
        )

        capability_surface = build_capability_surface(
            self.registry,
            session,
            scp_catalog=getattr(self.scp, "catalog", None),
        )
        capability_json = format_capability_surface_for_prompt(capability_surface)

        system = (
            "你是 MolMind Agent，面向 MASLD 低毒降脂分子筛选与科研旁证的能力助手。"
            "用简洁中文直接回答用户问题。"
            "回答能力/插件/技能/工具/MCP/Catalog 相关问题时，必须严格依据下方「当前能力面」；"
            "区分三态：已启用、可安装未启用、不可用。禁止编造未列出的能力。"
            "可安装的 SCP skill 需要用户确认安装后才能调用；安装成功后留在当前对话继续使用，"
            "不要要求用户把原请求再发一遍。"
            "不要声称「无法动态安装」或「只能使用固定不可扩展能力集」。"
            "SCP / MCP 实时结果只作补充证据，不得改写或重算冻结主榜。"
            "不要编造具体筛选排名或虚构实验数据。"
            "若用户追问已有排名，只解释最近对话中的冻结结果，"
            "不要声称重新筛选或重新导出。"
            "普通对话回复不得声称工具已经启动、即将立即启动或已经完成；"
            "只有本轮真实工具事件才能支持这些执行状态。"
            "若本轮属于普通对话但提到了已有筛选结果，只能使用下方提供的"
            "冻结结果数量；不得沿用较早轮次的 TopN，也不得声称该结果不存在。"
            "若用户其实想导出候选 CSV / 机制 PDF，可提示他们用自然语言描述产物；"
            "不要只回复『不调用工具』这类空话。"
            "当前会话绑定 SDF 时，本地 score_and_rank 工具可以执行实际筛选；"
            "绝不能声称当前环境不具备筛选/排序能力，或要求用户改到外部工具链。"
            "若「当前会话附件」已绑定 SDF，或能力面 session_library.has_sdf=true，"
            "严禁声称尚未绑定/缺少 SDF；应承认已绑定，并提示可继续筛选或导出。"
            "说明提名 CSV 字段时，必须严格依据能力面 nomination_csv："
            "schema_locked=true、user_selectable_columns=false；"
            "只能引用 columns_preview 中的真实列名，可说明还有更多锁定列；"
            "禁止编造 Rank/ID/SMILES/MASLD_Score/Toxicity_Risk/Note 等简化英文字段；"
            "禁止声称「执行时可以指定附加列」。"
            "讨论执行前可确认的选项时，只能使用 discussable_execution_options；"
            "旁证 enrich 是另跑流程，不改变提名 CSV schema。"
            "非 SDF 附件（PDF/图片/文档）仅作上下文参考，不能用于 score_and_rank，"
            "也不能假装已经解析了 PDF/图片正文。"
            f"当前会话附件：{'已绑定 ' + name if has_sdf else '无'}（仅本会话可用，不跨会话）。"
            f"{frozen_context}。"
            f"\n当前能力面（JSON）：{capability_json}"
            + (f"\n{attachment_context}" if attachment_context else "")
        )
        user = (
            f"最近对话：\n{history}\n\n"
            f"会话工作记忆（最近的调用、观察与 Loop 决策）：\n{working_context}\n\n"
            f"若本轮由指引触发，以下是可审计的恢复上下文；只复用明确成功且输入未变化的结果：\n"
            f"{resume_context}\n\n"
            f"用户本轮：{text}\n\n"
            "请直接回答本轮问题。若在说明能力清单，按已启用 / 可安装 分层组织，"
            "并提示用户如何启用未装项。"
        )
        try:
            for delta in chat_completion_stream(settings, system=system, user=user):
                if self._run_controller(session).interruption_requested:
                    raise RunInterrupted("user_guidance")
                yield delta
        except MechanismLLMError:
            raise
        session.working_memory.append(
            {
                "kind": "capability_surface_answer",
                "user_text": str(text or "")[:240],
                "installed_plugin_ids": [
                    item.get("plugin_id")
                    for item in capability_surface.get("installed_plugins") or []
                    if item.get("installed")
                ],
                "available_skill_ids": [
                    item.get("skill_id")
                    for item in capability_surface.get("available_skills") or []
                ],
                "installable_scp_skill_ids": [
                    item.get("skill_id")
                    for item in capability_surface.get("installable_scp_skills") or []
                    if not item.get("enabled")
                ],
                "recorded_at_unix": int(time.time()),
            }
        )
        session.working_memory = session.working_memory[-24:]

    def _chat_reply_fallback(self, session: AgentSession, text: str) -> str:
        """Offline/LLM-fail path: surface-aware short reply."""
        from agent.runtime.capability_context import build_capability_surface

        has_sdf = bool(session.sdf_bytes)
        name = session.sdf_filename or "化合物库.sdf"
        surface = build_capability_surface(
            self.registry,
            session,
            scp_catalog=getattr(self.scp, "catalog", None),
        )
        skill_titles = [
            str(item.get("title") or item.get("skill_id") or "")
            for item in surface.get("available_skills") or []
        ][:4]
        installable = [
            str(item.get("title") or item.get("skill_id") or "")
            for item in surface.get("installable_scp_skills") or []
            if not item.get("enabled")
        ][:4]
        bits = []
        if skill_titles:
            bits.append("当前已启用：" + "、".join(skill_titles))
        if installable:
            bits.append(
                "可安装（需确认）："
                + "、".join(installable)
                + "。确认安装后即可调用。"
            )
        surface_line = "；".join(bits)
        csv_fact = surface.get("nomination_csv") or {}
        csv_preview = list(csv_fact.get("columns_preview") or [])[:5]
        asks_csv_schema = any(
            token in str(text or "").lower()
            for token in ("csv", "字段", "列名", "哪些列", "附加列")
        )
        if has_sdf:
            base = (
                f"收到。当前会话已绑定附件「{name}」（仅本会话可用）。"
                "你可以继续问概念，或直接说：生成 top10 候选清单 csv。"
            )
        else:
            base = (
                "收到。我可以回答化学/流程问题，也可以在你上传 .sdf 后帮你做筛选排序与机制报告。"
                "试试问一个具体概念，或上传附件后说目标产物。"
            )
        parts = [base]
        if surface_line:
            parts.append(surface_line)
        if asks_csv_schema and csv_preview:
            parts.append(
                "提名 CSV 列 schema 已锁定，不可自定义附加列；"
                f"真实列示例：{' / '.join(csv_preview)} 等。"
            )
        return "\n".join(parts)

    def _execute_tool_adapter(
        self,
        session: AgentSession,
        tool_id: str,
        args: dict[str, Any],
        *,
        event_context: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Validate, budget and dispatch one Registry-backed tool call."""
        tool = self.registry.tools.get(tool_id)
        if tool is None:
            decision = self._authorize_tool_call(session, tool_id, args)
            yield from self._governance_denied_events(
                session,
                tool_id=tool_id,
                decision=decision,
            )
            return
        decision = self._authorize_tool_call(session, tool_id, args)
        if not decision.allowed:
            yield from self._governance_denied_events(
                session,
                tool_id=tool_id,
                decision=decision,
            )
            return
        retry_of = str((session.active_run or {}).get("retry_of_run_id") or "")
        reusable = next(
            (
                item
                for item in reversed(session.tool_checkpoints)
                if retry_of
                and item.get("run_id") == retry_of
                and item.get("tool") == tool_id
                and item.get("args_hash") == decision.args_hash
                and item.get("status") == "succeeded"
                and not bool(getattr(tool, "writes_selection", False))
            ),
            None,
        )
        if reusable is not None:
            yield self._emit(
                session,
                {
                    "type": "tool_start",
                    "tool": tool_id,
                    "plugin": tool.plugin_id,
                    "args": copy.deepcopy(args),
                    "checkpoint_reused": True,
                    "reused_from_checkpoint_id": reusable.get("checkpoint_id") or "",
                    **dict(event_context or {}),
                },
            )
            terminal = copy.deepcopy(reusable.get("terminal_event") or {})
            terminal.update(
                {
                    "type": "tool_end",
                    "tool": tool_id,
                    "ok": True,
                    "status": "succeeded",
                    "checkpoint_reused": True,
                    "reused_from_checkpoint_id": reusable.get("checkpoint_id") or "",
                }
            )
            yield self._emit(session, terminal)
            return
        method = getattr(self, f"_execute_{tool_id}", None)
        if not callable(method):
            if tool.plugin_id == "scp-hub":
                yield self._emit(
                    session,
                    {
                        "type": "tool_start",
                        "tool": tool.tool_id,
                        "plugin": "scp-hub",
                        "source": "scp-hub",
                        "args": {
                            **args,
                            "writes_selection": False,
                            "participates_in_ranking": False,
                        },
                        **dict(event_context or {}),
                    },
                )
                yield self._execute_scp_tool(session, tool, args)
                return
            yield from self._governance_denied_events(
                session,
                tool_id=tool_id,
                decision=type(decision)(
                    allowed=False,
                    code="adapter_missing",
                    message=f"工具 {tool_id} 尚无运行时适配器",
                    args_hash=decision.args_hash,
                    call=None,
                    approval_scope=decision.approval_scope,
                ),
            )
            return
        yield from method(session, **args)

    def _execute_scp_tool(self, session: AgentSession, tool: Any, args: dict[str, Any]) -> dict[str, Any]:
        """Execute a dynamic SCP tool and preserve the frozen-ranking boundary."""
        controller = self._run_controller(session)
        if float(tool.timeout_sec or 0) > 120:
            skill_id = next((sid for sid, state in session.installed_scp_skills.items() if tool.tool_id in state.get("tools", [])), "")
            job = self.scp_jobs.submit(
                lambda: self.scp.call(session, tool.tool_id, args, allow_live=True),
                session_id=session.session_id,
                skill_id=skill_id,
                tool_id=tool.tool_id,
                run_id=controller.run_id,
                arguments=args,
                allow_live=True,
            )
            return self._emit(session, {"type":"tool_end","tool":tool.tool_id,"ok":True,"status":"queued","job_id":job["job_id"],"source":"scp-hub","participates_in_ranking":False,"ranking_changed":False,"writes_selection":False})
        try:
            observation = run_cancellable(
                lambda: self.scp.call(session, tool.tool_id, args, allow_live=True),
                cancel_event=controller.cancel_event,
                expected_run_id=controller.run_id,
                current_run_id=lambda: self._run_controller(session).run_id,
            )
            if controller.interruption_requested:
                return self._emit(session, {"type": "tool_end", "tool": tool.tool_id, "ok": False, "status": "interrupted", "error_code": "user_guidance", "source": "scp-hub", "participates_in_ranking": False, "ranking_changed": False, "writes_selection": False})
            digest = self._scp_observation_digest(observation)
            return self._emit(session, {"type": "tool_end", "tool": tool.tool_id, "ok": observation.status in {"hit", "cache_hit"}, "status": "succeeded" if observation.status in {"hit", "cache_hit"} else "failed", "source": "scp-hub", "summary": f"SCP Hub 返回 {len(digest.get('content') or [])} 个可用结果块", "digest": digest, "participates_in_ranking": False, "ranking_changed": False, "writes_selection": False})
        except CallCancelled:
            return self._emit(session, {"type": "tool_end", "tool": tool.tool_id, "ok": False, "status": "interrupted", "error_code": "user_guidance", "source": "scp-hub", "participates_in_ranking": False, "ranking_changed": False, "writes_selection": False})
        except Exception as exc:  # remote failures are never scientific negatives
            code = getattr(exc, "code", "tool_failed")
            return self._emit(session, {"type": "tool_end", "tool": tool.tool_id, "ok": False, "error_code": code, "error": str(exc), "source": "scp-hub", "participates_in_ranking": False, "ranking_changed": False, "writes_selection": False})

    @staticmethod
    def _scp_observation_digest(observation: Any, *, limit: int = 9000) -> dict[str, Any]:
        """Keep a bounded, synthesizable SCP payload in the canonical envelope."""
        content: list[str] = []
        remaining = max(1000, int(limit))
        for block in getattr(observation, "content", None) or []:
            value = getattr(block, "value", "")
            if isinstance(value, str):
                rendered = value.strip()
            else:
                rendered = json.dumps(value, ensure_ascii=False, default=str)
            if not rendered:
                continue
            rendered = rendered[:remaining]
            content.append(rendered)
            remaining -= len(rendered)
            if remaining <= 0:
                break
        citations_raw = list(getattr(observation, "citations", None) or [])[:10]
        citations = [
            AgentRuntime._compact_observation(
                json.dumps(item, ensure_ascii=False, default=str), limit=300
            )
            for item in citations_raw
        ]
        return {
            "source": "scp-hub",
            "server_id": str(getattr(observation, "server_id", "") or ""),
            "tool_name": str(getattr(observation, "tool_name", "") or ""),
            "skill_id": str(getattr(observation, "skill_id", "") or ""),
            "status": str(getattr(observation, "status", "") or ""),
            "cache_status": str(getattr(observation, "cache_status", "") or "unknown"),
            "response_hash": str(getattr(observation, "response_hash", "") or ""),
            "citations": citations,
            "content": content,
            "writes_selection": False,
            "participates_in_ranking": False,
        }

    def _execute_scp_recovery_step(
        self,
        session: AgentSession,
        tool_id: str,
        arguments: dict[str, Any],
        *,
        capability_id: str,
        evidence_role: str,
        recovery_stage: str,
    ) -> Iterator[dict[str, Any]]:
        """Execute one recovery call and return its terminal tool event."""
        terminal: dict[str, Any] = {}
        for event in self._execute_tool_adapter(
            session,
            tool_id,
            arguments,
            event_context={
                "capability_id": capability_id,
                "evidence_role": evidence_role,
                "recovery_stage": recovery_stage,
                "claim_scopes": self.task_router.claim_scopes(capability_id),
            },
        ):
            if event.get("type") == "tool_end":
                terminal = event
            yield event
        return terminal

    def _recover_scp_observation(
        self,
        session: AgentSession,
        *,
        question: str,
        capability_id: str,
        enabled_skill_ids: set[str],
        include_fallback: bool = True,
        initial_digest: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Run plugin-declared probes, relaxed queries and cross-Skill fallback."""
        steps, fallback = self.task_router.recovery_steps(
            capability_id,
            question,
            enabled_skill_ids=enabled_skill_ids,
        )
        if not include_fallback:
            fallback = None
        if not steps and fallback is None:
            return None
        yield self._emit(
            session,
            {
                "type": "agent_replan",
                "action": "recover_observation",
                "reason": "initial_observation_irrelevant_or_empty",
                "capability_id": capability_id,
                "steps": [
                    {
                        "tool": step["tool_id"],
                        "title": step["title"],
                        "evidence_role": step["evidence_role"],
                    }
                    for step in steps
                ]
                + (
                    [
                        {
                            "tool": fallback.tool_id,
                            "title": fallback.label,
                            "evidence_role": "fallback_evidence",
                        }
                    ]
                    if fallback is not None
                    else []
                ),
            },
        )
        trace: list[dict[str, Any]] = []
        last_result: dict[str, Any] | None = None
        candidate_values: list[str] = []
        candidate_hashes: list[str] = []
        for index, step in enumerate(steps, start=1):
            yield self._emit(
                session,
                {
                    "type": "thinking",
                    "text": f"初始机制证据不足，正在执行恢复步骤 {index}：{step['title']}。",
                },
            )
            end = yield from self._execute_scp_recovery_step(
                session,
                step["tool_id"],
                step["arguments"],
                capability_id=capability_id,
                evidence_role=step["evidence_role"],
                recovery_stage=step["title"],
            )
            digest = ((end.get("observation") or {}).get("digest") or end.get("digest") or {})
            record = {
                "stage": step["title"],
                "tool_id": step["tool_id"],
                "evidence_role": step["evidence_role"],
                "ok": bool(end.get("ok")),
                "status": digest.get("status") or end.get("status") or "failed",
                "cache_status": digest.get("cache_status") or "unknown",
                "response_hash": digest.get("response_hash") or "",
            }
            trace.append(record)
            if not end.get("ok") or step["evidence_role"] != "evidence_query":
                continue
            values = [
                str(value)
                for value in digest.get("content", [])
                if str(value).strip()
            ]
            assessment = self.observation_validator.validate(
                plugin_id="scp-hub",
                capability_id=capability_id,
                question=question,
                values=values,
            )
            digest["relevance"] = assessment.as_dict()
            digest["degraded_channels"] = assessment.degraded_channels
            digest["recovery"] = {"trace": list(trace), "exhausted": False}
            digest["recovery_primary"] = dict(initial_digest or {})
            record["relevance_status"] = assessment.status
            record["relevance_score"] = assessment.score
            if values and assessment.score > 0:
                candidate_values.extend(values)
                if digest.get("response_hash"):
                    candidate_hashes.append(str(digest["response_hash"]))
            yield self._emit(
                session,
                {
                    "type": "observation_validation",
                    "source": "scp-hub",
                    **assessment.as_dict(),
                    "recovery_stage": step["title"],
                },
            )
            last_result = {
                "values": values,
                "digest": digest,
                "assessment": assessment,
                "label": "机制关系查询",
            }
            if assessment.relevant:
                return last_result

        if fallback is not None:
            yield self._emit(
                session,
                {
                    "type": "thinking",
                    "text": "知识图谱恢复查询仍未形成直接证据，正在通过文献检索 Skill 补证。",
                },
            )
            end = yield from self._execute_scp_recovery_step(
                session,
                fallback.tool_id,
                fallback.arguments,
                capability_id=fallback.capability_id,
                evidence_role="fallback_evidence",
                recovery_stage=fallback.label,
            )
            digest = ((end.get("observation") or {}).get("digest") or end.get("digest") or {})
            record = {
                "stage": fallback.label,
                "tool_id": fallback.tool_id,
                "evidence_role": "fallback_evidence",
                "ok": bool(end.get("ok")),
                "status": digest.get("status") or end.get("status") or "failed",
                "cache_status": digest.get("cache_status") or "unknown",
                "response_hash": digest.get("response_hash") or "",
            }
            trace.append(record)
            if end.get("ok"):
                values = [
                    str(value)
                    for value in digest.get("content", [])
                    if str(value).strip()
                ]
                assessment = self.observation_validator.validate(
                    plugin_id="scp-hub",
                    capability_id=fallback.capability_id,
                    question=question,
                    values=values,
                )
                digest["relevance"] = assessment.as_dict()
                digest["degraded_channels"] = assessment.degraded_channels
                digest["evidence_mode"] = "literature_fallback"
                digest["recovery"] = {"trace": list(trace), "exhausted": False}
                digest["recovery_primary"] = dict(initial_digest or {})
                record["relevance_status"] = assessment.status
                record["relevance_score"] = assessment.score
                yield self._emit(
                    session,
                    {
                        "type": "observation_validation",
                        "source": "scp-hub",
                        **assessment.as_dict(),
                        "recovery_stage": fallback.label,
                        "fallback_for": capability_id,
                    },
                )
                last_result = {
                    "values": values,
                    "digest": digest,
                    "assessment": assessment,
                    "label": "机制关系查询（文献补证）",
                }
                if assessment.relevant:
                    return last_result
                combined_values = [*candidate_values, *values]
                combined_assessment = self.observation_validator.validate(
                    plugin_id="scp-hub",
                    capability_id=capability_id,
                    question=question,
                    values=combined_values,
                )
                if combined_assessment.relevant:
                    hashes = [
                        *candidate_hashes,
                        *(
                            [str(digest.get("response_hash"))]
                            if digest.get("response_hash")
                            else []
                        ),
                    ]
                    fusion_hash = hashlib.sha256(
                        "|".join(hashes).encode("utf-8")
                    ).hexdigest()
                    trace.append(
                        {
                            "stage": "多源证据联合校验",
                            "evidence_role": "evidence_fusion",
                            "ok": True,
                            "status": combined_assessment.status,
                            "response_hash": f"sha256:{fusion_hash}",
                        }
                    )
                    digest = {
                        **digest,
                        "server_id": "SciGraph-Bio+Scholar-KG",
                        "tool_name": "recovery_evidence_fusion",
                        "skill_id": "mechanism_research+literature_research",
                        "response_hash": f"sha256:{fusion_hash}",
                        "content": combined_values,
                        "relevance": combined_assessment.as_dict(),
                        "degraded_channels": [],
                        "evidence_mode": "combined_recovery",
                        "recovery": {"trace": list(trace), "exhausted": False},
                        "recovery_primary": dict(initial_digest or {}),
                    }
                    yield self._emit(
                        session,
                        {
                            "type": "observation_fusion_validation",
                            "source": "scp-hub",
                            **combined_assessment.as_dict(),
                            "component_response_hashes": hashes,
                            "response_hash": f"sha256:{fusion_hash}",
                            "fallback_for": capability_id,
                        },
                    )
                    return {
                        "values": combined_values,
                        "digest": digest,
                        "assessment": combined_assessment,
                        "label": "机制关系查询（图谱与文献联合补证）",
                    }

        if last_result is not None:
            last_result["digest"]["recovery"] = {
                "trace": trace,
                "exhausted": True,
            }
            last_result["digest"]["recovery_primary"] = dict(initial_digest or {})
            return last_result
        return None

    def _display_scp_concept(self, canonical: str) -> str:
        plugin = self.registry.plugins.get("scp-hub")
        terminology = getattr(plugin, "terminology", {}) if plugin else {}
        for canonical_map in terminology.values():
            if not isinstance(canonical_map, dict) or canonical not in canonical_map:
                continue
            aliases = [str(value) for value in canonical_map.get(canonical) or []]
            preferred = next(
                (
                    value
                    for value in aliases
                    if re.search(r"[\u3400-\u9fffα-ωΑ-Ω]", value)
                ),
                "",
            )
            return preferred or canonical
        return canonical

    def _display_scp_reason(self, reason: str) -> str:
        if reason == "observation_empty_result":
            return "远程查询结果为空"
        if reason == "observation_empty":
            return "远程 Observation 为空"
        if reason == "observation_scope_missing":
            return "当前问题没有可继承或可识别的科学范围"
        if reason.startswith("missing_concept:"):
            return f"未覆盖{self._display_scp_concept(reason.split(':', 1)[1])}"
        if reason.startswith("time_range_not_met:"):
            return f"未满足 {reason.split(':', 1)[1]} 年后的时间范围"
        if reason.startswith("excluded_concept_present:"):
            return f"包含已排除主题{self._display_scp_concept(reason.split(':', 1)[1])}"
        return reason

    @staticmethod
    def _parse_scp_payload(value: str) -> Any:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    @classmethod
    def _iter_scp_output_items(cls, values: list[str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for raw in values:
            payload = cls._parse_scp_payload(str(raw or ""))
            if isinstance(payload, dict):
                output = payload.get("output")
                if isinstance(output, list) and output:
                    for item in output:
                        if isinstance(item, dict):
                            items.append(item)
                    continue
                data = payload.get("data")
                if isinstance(data, list) and data:
                    for item in data:
                        if isinstance(item, dict):
                            items.append(item)
                        else:
                            items.append({"summary": str(item)})
                    continue
                if any(
                    key in payload
                    for key in (
                        "paper_title",
                        "title",
                        "abstract",
                        "summary",
                        "node_text",
                    )
                ):
                    items.append(payload)
                    continue
            text = str(raw or "").strip()
            if text:
                items.append({"summary": text})
        return items

    @classmethod
    def _format_scp_evidence_materials(
        cls,
        values: list[str],
        *,
        limit_items: int = 8,
        snippet_chars: int = 280,
    ) -> str:
        """Organize remote observation items for user-facing replies."""
        items = cls._iter_scp_output_items(values)
        if not items:
            return ""
        lines: list[str] = ["## 检索到的资料", ""]
        shown = 0
        for item in items:
            if shown >= limit_items:
                break
            title = str(
                item.get("paper_title")
                or item.get("title")
                or item.get("name")
                or item.get("node_text")
                or ""
            ).strip()
            year = str(
                item.get("pub_year")
                or item.get("year")
                or item.get("publication_year")
                or ""
            ).strip()
            authors = item.get("authors") or item.get("author") or ""
            if isinstance(authors, list):
                authors = "、".join(str(part).strip() for part in authors if str(part).strip())
            else:
                authors = str(authors or "").strip()
            snippet = str(
                item.get("abstract")
                or item.get("summary")
                or item.get("node_text")
                or item.get("text")
                or ""
            ).strip()
            if not title and not snippet:
                continue
            shown += 1
            heading = title or f"资料 {shown}"
            if year and year not in heading:
                heading = f"{heading}（{year}）"
            lines.append(f"{shown}. **{heading}**")
            if authors:
                lines.append(f"   - 作者：{authors}")
            if snippet and snippet != title:
                compact = cls._compact_observation(snippet, limit=snippet_chars)
                lines.append(f"   - 摘要/要点：{compact}")
            doi = str(item.get("doi") or item.get("DOI") or "").strip()
            if doi:
                lines.append(f"   - DOI：{doi}")
            lines.append("")
        if shown == 0:
            # Fall back to raw evidence blocks when structured fields are absent.
            evidence = "\n\n".join(str(value).strip() for value in values if str(value).strip())
            if not evidence:
                return ""
            return (
                "## 检索到的资料\n\n"
                f"```text\n{cls._compact_observation(evidence, limit=6000)}\n```"
            )
        omitted = max(0, len(items) - shown)
        if omitted:
            lines.append(f"（另有 {omitted} 条未完整展开）")
            lines.append("")
        return "\n".join(lines).rstrip()

    def _scp_relevance_failure_footer(
        self,
        *,
        label: str,
        relevance: dict[str, Any],
        digest: dict[str, Any],
        question: str,
    ) -> str:
        missing = "、".join(
            self._display_scp_concept(str(value))
            for value in relevance.get("missing_concepts") or []
        ) or "目标约束"
        reasons = "；".join(
            self._display_scp_reason(str(value))
            for value in relevance.get("reasons") or []
        )
        recovery = digest.get("recovery") or {}
        recovery_note = (
            "\n\nAgent 已依次执行图谱探测、放宽机制查询和可用的文献补证，"
            "但仍没有获得满足全部问题约束的直接证据。"
            if recovery.get("exhausted")
            else ""
        )
        subquestions = re.findall(
            r"(?:^|\n)\s*(\d+)[.、]\s*([^\n]+)", str(question or "")
        )
        subquestion_note = (
            "\n\n逐项结果：\n"
            + "\n".join(
                f"{number}. {body.strip()}：当前 Observation 未通过完整约束校验，不能据此下结论。"
                for number, body in subquestions
            )
            if subquestions
            else ""
        )
        return (
            f"## 相关性校验结论\n\n"
            f"SCP Hub 已完成{label}，但返回结果未通过相关性校验，"
            f"不能作为目标问题的直接证据。\n\n"
            f"缺失或未满足：{missing}。\n"
            f"校验原因：{reasons or 'evidence_relevance_insufficient'}。"
            f"{recovery_note}"
            f"{subquestion_note}"
            "\n\n本次实时资料不参与候选排序。"
        )

    def _synthesize_scp_reply(
        self,
        *,
        question: str,
        label: str,
        values: list[str],
        digest: dict[str, Any],
    ) -> str:
        """Answer from the actual MCP observation instead of a success banner."""
        evidence = "\n\n".join(values).strip()
        if not evidence:
            return (
                f"SCP Hub 已完成{label}，但返回内容为空，无法据此生成科学总结。"
                "本次调用不参与候选排序。"
            )
        relevance = digest.get("relevance") or {}
        if relevance and not relevance.get("relevant", False):
            materials = self._format_scp_evidence_materials(values)
            footer = self._scp_relevance_failure_footer(
                label=label,
                relevance=relevance if isinstance(relevance, dict) else {},
                digest=digest,
                question=question,
            )
            try:
                from plugins.molmind_core.scientific.mechanism.llm_client import (
                    chat_completion,
                    resolve_llm_settings,
                )

                settings = resolve_llm_settings(
                    {"enabled": True, "agent_chat": True}, purpose="agent_chat"
                )
                if settings.ready:
                    system = (
                        "你是 MolMind 的科研检索整理器。任务是先整理远程返回的资料，"
                        "再给出相关性校验结论。规则："
                        "1) 只能依据给定 Observation 整理标题、年份、作者、摘要要点；"
                        "2) 不得补造论文、作者、年份或结论；"
                        "3) 明确说明这些资料未通过相关性校验，不能当作目标问题的直接证据；"
                        "4) 必须复述缺失概念与校验原因；"
                        "5) 末尾说明实时资料不参与候选排序；"
                        "6) 用清晰的中文 Markdown，先资料后结论。"
                    )
                    user = (
                        f"用户问题：{question}\n"
                        f"任务类型：{label}\n"
                        f"claim_scopes：{digest.get('claim_scopes') or []}\n"
                        f"相关性校验：{json.dumps(relevance, ensure_ascii=False)}\n"
                        f"SCP Observation：\n{evidence[:10000]}\n"
                        "请先整理检索资料，最后再总结校验结论。"
                    )
                    reply = chat_completion(settings, system=system, user=user).strip()
                    if reply:
                        # Ensure the hard governance footer is not dropped by the model.
                        if "不参与候选排序" not in reply and "不参与候选排名" not in reply:
                            reply = f"{reply.rstrip()}\n\n本次实时资料不参与候选排序。"
                        if "未通过相关性校验" not in reply:
                            reply = f"{reply.rstrip()}\n\n{footer}"
                        return reply
            except Exception:  # noqa: BLE001 - fall back to deterministic layout
                pass
            if materials:
                return f"{materials}\n\n{footer}"
            return footer
        protocol_check = digest.get("protocol_validation") or {}
        if protocol_check and not protocol_check.get("complete", False):
            missing = ", ".join(protocol_check.get("missing_fields") or []) or "未声明字段"
            materials = self._format_scp_evidence_materials(values)
            conclusion = (
                f"## 方案完整性结论\n\n"
                f"SCP Hub 已完成{label}，但方案 Observation 缺少必要结构字段：{missing}。\n\n"
                "当前内容只能作为不完整草案，不能当作可执行实验方案；"
                "本次实时资料不参与候选排序。"
            )
            if materials:
                return f"{materials}\n\n{conclusion}"
            return conclusion
        try:
            from plugins.molmind_core.scientific.mechanism.llm_client import (
                chat_completion,
                resolve_llm_settings,
            )

            settings = resolve_llm_settings(
                {"enabled": True, "agent_chat": True}, purpose="agent_chat"
            )
            if settings.ready:
                system = (
                    "你是 MolMind 的科研证据总结器。只能依据给定 SCP Hub Observation 回答，"
                    "不得补造论文、作者、年份、机制或实验结论。区分远程返回事实与推断；"
                    "若证据不足要明确说明。用清晰的中文 Markdown 输出，并在末尾说明"
                    "这些实时资料不参与候选排名。严格遵守 claim_scopes："
                    "experimental_design_advice 只能生成实验设计草案，不能证明或声称检索到了"
                    "论文、作者、年份、研究发现或机制事实；literature_evidence 才能支持文献结论；"
                    "mechanism_evidence 才能支持机制关系。"
                )
                user = (
                    f"用户问题：{question}\n"
                    f"任务类型：{label}\n"
                    f"Server：{digest.get('server_id') or 'unknown'}\n"
                    f"响应哈希：{digest.get('response_hash') or 'unknown'}\n"
                    f"claim_scopes：{digest.get('claim_scopes') or []}\n"
                    f"SCP Observation：\n{evidence[:10000]}\n"
                    "请直接回答用户问题。"
                )
                reply = chat_completion(settings, system=system, user=user).strip()
                if reply:
                    risks = protocol_check.get("risk_flags") or []
                    if risks:
                        warning = "；".join(str(item.get("message") or "需要科学复核") for item in risks)
                        return f"【科学复核提示】{warning}\n\n{reply}"
                    return reply
        except Exception:  # noqa: BLE001 - deterministic evidence fallback below
            pass
        materials = self._format_scp_evidence_materials(values)
        if materials:
            return (
                f"{materials}\n\n"
                "以上为远程返回内容整理（未经过模型扩写）。"
                "这些实时资料仅作补充，不参与候选排序。"
            )
        return (
            f"已通过 SCP Hub 完成{label}。以下为远程返回内容（未经过模型扩写）：\n\n"
            f"```text\n{evidence[:8000]}\n```\n\n"
            "这些实时资料仅作补充，不参与候选排序。"
        )

    def _execute_required_tool(
        self,
        session: AgentSession,
        tool_id: str,
        args: dict[str, Any],
    ) -> Iterator[dict[str, Any]]:
        """Yield one tool stream and return whether its terminal event succeeded."""
        succeeded = False
        saw_terminal = False
        for event in self._execute_tool_adapter(session, tool_id, args):
            if event.get("type") == "tool_end" and event.get("tool") == tool_id:
                saw_terminal = True
                succeeded = bool(event.get("ok"))
            yield event
        return saw_terminal and succeeded

    def _execute_score_and_rank(
        self, session: AgentSession, *, top_n: int
    ) -> Iterator[dict[str, Any]]:
        yield from self._run_nominate(session, top_n=top_n, export_primary=False)

    def _execute_export_nomination(
        self, session: AgentSession, *, tier: str = "primary"
    ) -> Iterator[dict[str, Any]]:
        if tier == "reserve":
            yield from self._export_reserve_only(session)
            return
        yield from self._export_primary_only(session)

    def _execute_start_mechanism_report(
        self, session: AgentSession
    ) -> Iterator[dict[str, Any]]:
        yield from self._run_mechanism(session)

    def _execute_get_mechanism_job(
        self, session: AgentSession
    ) -> Iterator[dict[str, Any]]:
        job_id = session.last_mechanism_job_id
        if not job_id:
            yield self._emit(
                session,
                {
                    "type": "tool_end",
                    "tool": "get_mechanism_job",
                    "ok": False,
                    "error_code": "missing_precondition",
                    "error": "尚无机制任务 id",
                },
            )
            yield self._emit(
                session,
                {
                    "type": "assistant",
                    "text": "尚无机制任务 id。可先试用 `@tool:start_mechanism_report`。",
                },
            )
            return
        yield self._emit(
            session,
            {
                "type": "tool_start",
                "tool": "get_mechanism_job",
                "plugin": "molmind-core",
                "args": {"job_id": job_id},
            },
        )
        job = get_job(job_id)
        status = str(job.get("status") or "unknown") if isinstance(job, dict) else "unknown"
        yield self._emit(
            session,
            {
                "type": "tool_end",
                "tool": "get_mechanism_job",
                "ok": job is not None,
                "error_code": "" if job is not None else "mechanism_job_missing",
                "error": "" if job is not None else "机制任务不存在",
                "digest": {"job_id": job_id, "status": status},
            },
        )
        yield self._emit(
            session,
            {
                "type": "assistant",
                "text": f"机制任务 `{job_id}` 状态：{status}。",
            },
        )

    def _execute_export_submission_bundle(
        self, session: AgentSession
    ) -> Iterator[dict[str, Any]]:
        yield from self._export_submission_bundle(session)

    def _run_nominate(
        self,
        session: AgentSession,
        *,
        top_n: int,
        export_primary: bool = True,
        export_reserve: bool = False,
    ) -> Iterator[dict[str, Any]]:
        assert session.sdf_bytes is not None
        yield self._emit(
            session,
            {
                "type": "tool_start",
                "tool": "score_and_rank",
                "plugin": "molmind-core",
                "args": {
                    "top_n": top_n,
                    "source": session.sdf_filename,
                    "use_snapshot": True,
                    "allow_live": False,
                },
            },
        )
        yield self._emit(
            session,
            {
                "type": "thinking",
                "text": "正在读取化合物库，依次排除不符合要求的分子，并综合活性、安全性和结构差异挑选候选…",
            },
        )

        log_events: list[dict[str, Any]] = []
        run_cancellation = self._run_controller(session).cancel_event

        def on_log(entry: RunLogEntry) -> None:
            if run_cancellation.is_set():
                raise RunInterrupted("user_guidance")
            log_events.append({"type": "log", **entry.to_dict()})

        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".sdf", delete=False) as tmp:
                tmp.write(session.sdf_bytes)
                tmp_path = Path(tmp.name)

            def _call():
                return run_score_and_rank(
                    tmp_path,
                    top_n=top_n,
                    source_filename=session.sdf_filename,
                    log_sink=on_log,
                )

            started = time.perf_counter()
            try:
                result = run_cancellable(
                    _call,
                    cancel_event=run_cancellation,
                    expected_run_id=self._run_controller(session).run_id,
                    current_run_id=lambda: self._run_controller(session).run_id,
                )
            except CallCancelled as exc:
                raise RunInterrupted("user_guidance") from exc
            elapsed = time.perf_counter() - started
            if run_cancellation.is_set():
                raise RunInterrupted("user_guidance")
        except Exception as exc:  # noqa: BLE001
            yield self._emit(
                session,
                {"type": "tool_end", "tool": "score_and_rank", "ok": False, "error": str(exc)},
            )
            yield self._emit(session, {"type": "error", "detail": f"筛选失败：{exc}"})
            return
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

        # 运行日志会同时记录中英文版本，界面只展示中文版本，避免同一信息重复出现。
        for ev in (event for event in log_events[-12:] if event.get("lang") == "zh"):
            yield self._emit(session, ev)

        session.last_result = result
        session.frozen_ranking = snapshot_from_result(result)
        session.last_run_id = result.run_id
        session.last_selection_sha256 = result.selection_sha256
        session.last_config_hash = result.config.config_hash
        session.last_input_sha256 = result.input_sha256
        session.last_molecule_index = _molecule_index_from_result(result)
        session.run_history = [
            entry
            for entry in session.run_history
            if str(entry.get("run_id") or "") != str(result.run_id)
        ]
        session.run_history.append(
            {
                "run_id": result.run_id,
                "top_n": len(getattr(result, "top_molecules", None) or []),
                "selection_sha256": result.selection_sha256,
                "config_hash": result.config.config_hash,
                "input_sha256": result.input_sha256,
                "source_filename": session.sdf_filename,
            }
        )
        session.run_history = session.run_history[-20:]
        self.store.persist(session)

        yield self._emit(
            session,
            {
                "type": "tool_end",
                "tool": "score_and_rank",
                "ok": True,
                "elapsed_s": round(elapsed, 3),
                "digest": {
                    "run_id": result.run_id,
                    "output_count": result.output_count,
                    "selection_sha256": result.selection_sha256,
                    "config_hash": result.config.config_hash,
                },
            },
        )

        if export_primary:
            yield from self._export_primary_only(session)
        if export_reserve:
            yield from self._export_reserve_only(session)

    def _run_catalog_enrichment(
        self, session: AgentSession, *, only_plugin: str | None = None
    ) -> Iterator[dict[str, Any]]:
        """对已安装 Catalog 适配器跑 enrichment pass；空壳跳过。"""
        sha_before = session.last_selection_sha256
        for plugin_id, result in iter_installed_enrichment(session):
            if only_plugin and plugin_id != only_plugin:
                continue
            tool = str(result.get("tool") or "catalog_enrich")
            yield self._emit(
                session,
                {
                    "type": "tool_start",
                    "tool": tool,
                    "plugin": plugin_id,
                    "args": {"writes_selection": False},
                },
            )
            degraded = list(result.get("degraded") or [])
            yield self._emit(
                session,
                {
                    "type": "thinking",
                    "text": result.get("message")
                    or f"Catalog {plugin_id} enrichment 完成（不改排名）。",
                },
            )
            yield self._emit(
                session,
                {
                    "type": "tool_end",
                    "tool": tool,
                    "plugin": plugin_id,
                    "ok": bool(result.get("ok", True)),
                    "digest": {
                        **(result.get("digest") or {}),
                        "writes_selection": False,
                        "degraded": degraded,
                        "selection_sha256_unchanged": session.last_selection_sha256
                        == sha_before,
                    },
                },
            )
        # 防御：enrichment 不得改写主榜哈希
        if session.last_selection_sha256 != sha_before:
            session.last_selection_sha256 = sha_before
            yield self._emit(
                session,
                {
                    "type": "error",
                    "detail": "检测到 Catalog 试图改写主榜哈希，已回滚（不应发生）。",
                },
            )

    def _run_mechanism(self, session: AgentSession) -> Iterator[dict[str, Any]]:
        ensure_session_last_result(session)
        result = session.last_result
        if result is None:
            yield self._emit(session, {"type": "error", "detail": "尚无筛选结果，无法生成机制 PDF。"})
            return

        yield self._emit(
            session,
            {
                "type": "tool_start",
                "tool": "start_mechanism_report",
                "plugin": "molmind-core",
                "args": {"run_id": result.run_id},
            },
        )
        yield self._emit(
            session,
            {
                "type": "thinking",
                "text": "排名已冻结。正在生成机制假说与 HepG2-FFA 验证方案 PDF…",
            },
        )

        try:
            job_id = start_mechanism_job(
                result.top_molecules,
                llm_cfg=result.config.llm,
                mark_degraded=result.config.mark_degraded,
                source_filename=result.source_filename or session.sdf_filename,
                assumptions=result.config.assumptions,
                run_context={
                    "run_id": result.run_id,
                    "agent_run_id": self._run_controller(session).run_id,
                    "input_sha256": result.input_sha256,
                    "config_hash": result.config.config_hash,
                    "selection_sha256": result.selection_sha256,
                },
                mechanism_graphs=result.mechanism_graphs,
            )
        except Exception as exc:  # noqa: BLE001
            yield self._emit(
                session,
                {
                    "type": "tool_end",
                    "tool": "start_mechanism_report",
                    "ok": False,
                    "error": str(exc),
                },
            )
            yield self._emit(session, {"type": "error", "detail": f"启动机制报告失败：{exc}"})
            return

        session.last_mechanism_job_id = job_id
        deadline = time.time() + 180.0
        last_status = ""
        run_cancellation = self._run_controller(session).cancel_event
        while time.time() < deadline:
            if run_cancellation.is_set():
                cancel_job(job_id, reason="user_guidance")
                raise RunInterrupted("user_guidance")
            job = get_job(job_id)
            if not job:
                yield self._emit(
                    session,
                    {
                        "type": "tool_end",
                        "tool": "start_mechanism_report",
                        "ok": False,
                        "error_code": "mechanism_job_missing",
                        "error": "机制任务丢失",
                        "digest": {"job_id": job_id},
                    },
                )
                yield self._emit(session, {"type": "error", "detail": "机制任务丢失。"})
                return
            status = str(job.get("status") or "")
            if status != last_status:
                last_status = status
                yield self._emit(session, {"type": "thinking", "text": f"机制报告状态：{status}"})
            if status in {"cancel_requested", "cancelled"}:
                yield self._emit(session, {"type": "tool_end", "tool": "start_mechanism_report", "ok": False, "status": "interrupted", "error_code": "user_guidance", "digest": {"job_id": job_id, "status": status}})
                return
            if status == "ready":
                b64 = job.get("mechanism_pdf_base64") or ""
                pdf_name = job.get("mechanism_pdf_name") or f"mechanism_{result.run_id[:8]}.pdf"
                if not b64:
                    yield self._emit(session, {"type": "error", "detail": "机制 PDF 为空。"})
                    return
                content = base64.b64decode(b64)
                art = Artifact(
                    artifact_id=_aid(),
                    kind="pdf",
                    filename=pdf_name,
                    title="机制与验证方案",
                    subtitle=f"{pdf_name} · run_id={result.run_id[:8]}…",
                    media_type="application/pdf",
                    content=content,
                )
                self.store.put_artifact(session, art)
                yield self._emit(
                    session,
                    {
                        "type": "tool_end",
                        "tool": "start_mechanism_report",
                        "ok": True,
                        "digest": {
                            "job_id": job_id,
                            "status": "ready",
                            "artifact_id": art.artifact_id,
                        },
                    },
                )
                yield self._emit(
                    session,
                    {
                        "type": "card",
                        "card": {
                            "kind": "pdf",
                            "title": art.title,
                            "subtitle": art.subtitle,
                            "filename": art.filename,
                            "artifact_id": art.artifact_id,
                            "download_url": (
                                f"/api/agent/sessions/{session.session_id}/artifacts/"
                                f"{art.artifact_id}/download"
                            ),
                        },
                    },
                )
                return
            if status == "error":
                error_message = str(job.get("error") or "unknown")
                yield self._emit(
                    session,
                    {
                        "type": "tool_end",
                        "tool": "start_mechanism_report",
                        "ok": False,
                        "error_code": "mechanism_job_failed",
                        "error": error_message,
                        "digest": {"job_id": job_id, "status": status},
                    },
                )
                yield self._emit(
                    session,
                    {
                        "type": "error",
                        "detail": f"机制 PDF 生成失败：{error_message}",
                    },
                )
                return
            wait_interruptible(run_cancellation, timeout_sec=1.0, slice_sec=0.25)

        yield self._emit(
            session,
            {
                "type": "tool_end",
                "tool": "start_mechanism_report",
                "ok": False,
                "error_code": "mechanism_job_timeout",
                "error": "等待机制 PDF 超时",
                "digest": {"job_id": job_id, "status": last_status or "unknown"},
            },
        )
        yield self._emit(
            session,
            {"type": "error", "detail": "等待机制 PDF 超时；可稍后重试「只要 pdf」。"},
        )


_RUNTIME: AgentRuntime | None = None


def get_runtime() -> AgentRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = AgentRuntime()
    return _RUNTIME
