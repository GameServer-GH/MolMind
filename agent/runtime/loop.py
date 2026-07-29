"""Agent Loop：意图 → Skill 计划 → Tool 调用 → 流式事件（可落盘）。"""

from __future__ import annotations

import base64
import copy
import io
import json
import queue
import re
import tempfile
import threading
import time
import uuid
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
from agent.memory import STORE, AgentSession, Artifact, FileRunStore
from agent.policy import claim_ceiling_default
from agent.registry import get_registry
from agent.runtime.decide import llm_json_decision
from agent.runtime.governance import (
    GovernanceDecision,
    ToolGovernance,
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
from agent.runtime.scheduler import RunBudget, RunController
from agent.runtime.task_graph import TaskGraph
from agent.runtime.verification import evidence_correction, verify_assistant_claims
from plugins.catalog_dispatch import (
    TOOL_HANDLERS,
    dispatch_tool,
    iter_installed_enrichment,
)
from plugins.molmind_core.tools.scientific import run_score_and_rank, timed_call
from plugins.molmind_core.scientific.mechanism.jobs import get_job, start_mechanism_job
from plugins.molmind_core.scientific.pipeline.export import reserve_shortage_note
from plugins.molmind_core.scientific.pipeline.run_log import RunLogEntry
from plugins.molmind_core.scientific.evidence_gateway.contract import content_sha256


def _aid() -> str:
    return uuid.uuid4().hex[:12]


_EVIDENCE_MENTION_IDS = frozenset({"query_evidence", "masld_explain"})
_DEFAULT_CONFIG_EXECUTION_RE = re.compile(
    r"(?:使用|按).{0,12}(?:当前)?默认.{0,20}(?:MASLD|筛选|配置).{0,32}"
    r"(?:生成|导出|筛选|运行|top)",
    re.I,
)
_DIRECT_DELIVERABLE_RE = re.compile(
    r"(?:生成|导出|筛选|运行|重跑|重新跑|开始跑|制作|做一份|出一份)|"
    r"(?:希望|想要|需要|请|给我).{0,28}"
    r"(?:csv|候选|提名|清单|短名单|候补|结果包|top)",
    re.I,
)
_PENDING_TOP_N_REPLY_RE = re.compile(r"^\s*(?:top\s*)?(\d{1,3})\s*(?:个|名)?\s*$", re.I)
_PENDING_AFFIRM_RE = re.compile(r"^\s*(?:需要|要|是|对|可以|好|好的|行|继续|开始|现在呢|现在可以了?)(?:[。！!？?])?\s*$", re.I)
_PENDING_STATUS_RE = re.compile(r"(?:好了(?:吗|嘛)?|完成了?(?:吗|嘛)?|进度|开始了?(?:吗|嘛)?|还没好|怎么样了)", re.I)
_PENDING_CANCEL_RE = re.compile(r"^\s*(?:取消|算了|不用了?|不要了?|停止)(?:[。！!])?\s*$", re.I)
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
    return bool(
        _DEFAULT_CONFIG_EXECUTION_RE.search(text or "")
        or _DIRECT_DELIVERABLE_RE.search(text or "")
    )


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


class AgentRuntime:
    def __init__(self, store: FileRunStore | None = None) -> None:
        self.store = store or STORE
        self.registry = get_registry()
        # One mutable AgentSession is shared by all HTTP streams for its id.
        # A session lock preserves user-turn order while allowing unrelated
        # sessions to use the worker pool concurrently.
        self._session_locks: dict[str, threading.Lock] = {}
        self._session_locks_guard = threading.Lock()
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
        return self.store.get(session_id)

    def _session_lock(self, session_id: str) -> threading.Lock:
        with self._session_locks_guard:
            return self._session_locks.setdefault(session_id, threading.Lock())

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

    def attach_sdf(self, session: AgentSession, *, filename: str, content: bytes) -> None:
        session.sdf_bytes = content
        session.sdf_filename = filename or "library.sdf"
        session.sdf_ui_pending = True
        session.last_result = None
        session.last_run_id = ""
        session.last_selection_sha256 = ""
        session.last_molecule_index = {}
        session.last_mechanism_job_id = ""
        session.active_plan = None
        self.store.save_sdf(session)

    def detach_sdf(self, session: AgentSession) -> None:
        session.last_result = None
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
        return self.registry.settings_view(
            profile_id=profile_id,
            installed_catalog=installed,
        )

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

    def _begin_agent_turn(self, session: AgentSession) -> RunController:
        profile = self.registry.get_profile(session.profile_id)
        controller = RunController(RunBudget.from_mapping(profile.budgets))
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

    def _emit(self, session: AgentSession, event: dict[str, Any]) -> dict[str, Any]:
        kind = str(event.get("type") or "")
        controller = self._run_controller(session)
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
        elif kind == "done":
            controller.complete()
            session.agent_run_state = controller.snapshot()
            event.setdefault("run", session.agent_run_state)

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

    def handle_message(self, session: AgentSession, text: str) -> Iterator[dict[str, Any]]:
        self._begin_agent_turn(session)
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
                    {"role": "user", "text": text, "attachments": turn_attachments}
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
            is_continuation = bool(
                number_match
                or _PENDING_AFFIRM_RE.fullmatch(compact)
                or _PENDING_STATUS_RE.search(compact)
            )
            if is_continuation and (missing_sdf or missing_top_n):
                self._prepare_turn(session, text)
                if _PENDING_STATUS_RE.search(compact):
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

            # A complete, explicit new deliverable supersedes the old one.
            # Other substantive text is classified normally but does not
            # silently erase the unfinished request.
            if _DIRECT_DELIVERABLE_RE.search(compact):
                session.pending_action = None

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
                intent = parse_intent(
                    text,
                    default_top_n=session.top_n,
                    top_n_min=lo2,
                    top_n_max=hi2,
                )
        # Tool-shaped surface text is still ambiguous: let the conversation
        # model classify the dialog act before executing anything.  The
        # structural parser only supplies candidate parameters and a safe
        # offline fallback; it is not the source of truth for follow-ups.
        if intent.wants_tools and not intent.mentions and not intent.query_evidence:
            is_ranking_followup, ranking_molecule_id = ranking_question_fallback(text)
            if is_ranking_followup:
                # A ranking introduction/explanation is never an implicit
                # request to export a similarly numbered TopN.  Route it
                # before optional LLM planning, which otherwise sees the
                # tool-shaped “top5” surface and may choose export.
                action, why = "explain_ranking", "deterministic_frozen_ranking_followup"
            elif _is_direct_deliverable_request(intent, text):
                action, why = "execute_tools", "deterministic_direct_deliverable"
            else:
                action, why = self._classify_request_action(session, text, intent)
            if action != "execute_tools":
                intent = replace(
                    intent,
                    want_csv=False,
                    want_pdf=False,
                    skill_ids=(),
                    wants_tools=False,
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

    def _classify_request_action(
        self, session: AgentSession, text: str, intent: AgentIntent
    ) -> tuple[str, str]:
        """Classify execute-vs-chat before a tool-shaped request is dispatched."""
        # Online path: the LLM plans against the live registry rather than a
        # fixed action prompt. Its output is schema- and precondition-checked
        # inside ``llm_plan_request``. The legacy classifier below is retained
        # only as an offline/compatibility fallback.
        planned, plan_status = llm_plan_request(
            text=text,
            recent_messages=session.messages,
            tools=self.registry.tools,
            skills=self.registry.skills,
            capabilities=session_capabilities(session),
            default_top_n=session.top_n,
        )
        if planned is not None:
            if planned.action == "execute":
                return "execute_tools", f"{plan_status};{planned.rationale}"
            if planned.action == "explain":
                return "explain_ranking", f"{plan_status};{planned.rationale}"
            if planned.action == "clarify":
                session.pending_goal = {
                    "goal": planned.goal,
                    "rationale": planned.rationale,
                    "source_text": text,
                    "reason": "tool_contract_missing_parameters",
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
                "已有冻结结果时，含“解释/说明/为什么/为何/为啥/原因/理由”并指向 TopN"
                " 的请求属于 explain_ranking，即使没有点名某个分子。"
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
        # Consume pending UI attachment into this turn (same-session SDF bytes stay).
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
            }
        )
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
        has_result = session.last_result is not None

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
            if intent.explain_ranking:
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
            if not reply:
                reply = self._llm_chat_reply(session, text)
            session.messages.append({"role": "assistant", "text": reply})
            yield out(
                {
                    "type": "task_end",
                    "task_id": "conversation",
                    "status": "succeeded",
                    "observation": {
                        "summary": self._compact_observation(reply, limit=1200)
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
            (
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
        result = session.last_result
        if result is None:
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
        result = session.last_result
        if result is None:
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

    def _llm_chat_reply(self, session: AgentSession, text: str) -> str:
        """Answer general questions with LLM; fall back to a short template."""
        try:
            from plugins.molmind_core.scientific.mechanism.llm_client import (
                MechanismLLMError,
                chat_completion,
                resolve_llm_settings,
            )

            settings = resolve_llm_settings({"enabled": True, "agent_chat": True}, purpose="agent_chat")
            if not settings.ready:
                return self._chat_reply_fallback(session, text)

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
            frozen_result = session.last_result
            frozen_count = len(getattr(frozen_result, "top_molecules", None) or [])
            frozen_run_id = str(getattr(frozen_result, "run_id", "") or "").strip()
            frozen_context = (
                f"本会话最近冻结结果：Top {frozen_count}"
                + (f"，run_id={frozen_run_id}" if frozen_run_id else "")
                if frozen_result is not None
                else "本会话最近冻结结果：无"
            )
            hist_lines: list[str] = []
            for m in session.messages[-8:-1]:
                role = m.get("role")
                body = (m.get("text") or "").strip()
                if role in {"user", "assistant"} and body:
                    hist_lines.append(f"{role}: {body[:400]}")
            history = "\n".join(hist_lines) if hist_lines else "（无）"
            working_context = self._compact_observation(
                json.dumps(
                    session.working_memory[-4:],
                    ensure_ascii=False,
                ),
                limit=2200,
            ) or "（无）"

            system = (
                "你是 MolMind Agent，面向 MASLD 低毒降脂分子筛选的能力助手。"
                "用简洁中文直接回答用户问题（化学概念、用法、流程等）。"
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
                "能力介绍只能承诺当前已注册流程：按默认 MASLD 配置筛选并排序、"
                "导出候选 CSV、基于冻结榜单生成机制 PDF、解释冻结排名。"
                "不得承诺任意属性统计、任意筛选阈值、指定靶点虚拟筛选或跨库对比，"
                "除非当前工具事件已经明确提供该能力。"
                f"当前会话附件：{'已绑定 ' + name if has_sdf else '无'}（仅本会话可用，不跨会话）。"
                f"{frozen_context}。"
            )
            user = (
                f"最近对话：\n{history}\n\n"
                f"会话工作记忆（最近的调用、观察与 Loop 决策）：\n{working_context}\n\n"
                f"用户本轮：{text}\n\n"
                "请直接回答本轮问题。"
            )
            return chat_completion(settings, system=system, user=user).strip()
        except Exception:  # noqa: BLE001 — LLM optional
            return self._chat_reply_fallback(session, text)

    def _chat_reply_fallback(self, session: AgentSession, text: str) -> str:
        """Offline/LLM-fail path: one generic reply — no topic keyword tables."""
        has_sdf = bool(session.sdf_bytes)
        name = session.sdf_filename or "化合物库.sdf"
        if has_sdf:
            return (
                f"收到。当前会话已绑定附件「{name}」（仅本会话可用）。"
                "你可以继续问概念，或直接说：生成 top10 候选清单 csv。"
            )
        return (
            "收到。我可以回答化学/流程问题，也可以在你上传 .sdf 后帮你做筛选排序与机制报告。"
            "试试问一个具体概念，或上传附件后说目标产物。"
        )

    def _execute_tool_adapter(
        self, session: AgentSession, tool_id: str, args: dict[str, Any]
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
        method = getattr(self, f"_execute_{tool_id}", None)
        if not callable(method):
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

        def on_log(entry: RunLogEntry) -> None:
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

            result, elapsed = timed_call(_call)
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
        while time.time() < deadline:
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
            time.sleep(1.0)

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
