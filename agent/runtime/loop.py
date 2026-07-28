"""Agent Loop：意图 → Skill 计划 → Tool 调用 → 流式事件（可落盘）。"""

from __future__ import annotations

import base64
import copy
import queue
import re
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, is_dataclass, replace
from pathlib import Path
from typing import Any, Iterator

from agent.intent import AgentIntent, MentionRef, parse_intent
from agent.memory import STORE, AgentSession, Artifact, FileRunStore
from agent.policy import claim_ceiling_default
from agent.registry import get_registry
from agent.runtime.decide import llm_json_decision
from agent.runtime.reply import format_run_completion
from plugins.catalog_dispatch import (
    TOOL_HANDLERS,
    dispatch_tool,
    iter_installed_enrichment,
)
from plugins.molmind_core.tools.scientific import run_score_and_rank, timed_call
from plugins.molmind_core.scientific.mechanism.jobs import get_job, start_mechanism_job
from plugins.molmind_core.scientific.pipeline.run_log import RunLogEntry
from plugins.molmind_core.scientific.evidence_gateway.contract import content_sha256


def _aid() -> str:
    return uuid.uuid4().hex[:12]


_EVIDENCE_MENTION_IDS = frozenset({"query_evidence", "masld_explain"})
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
    """Deep-copy and digest the complete mutable selection surface."""

    if result is None:
        return {}, content_sha256({})
    names = (
        "top_molecules",
        "reserve_molecules",
        "scored_molecules",
        "selection_sha256",
    )
    snapshot = {
        name: copy.deepcopy(getattr(result, name))
        for name in names
        if hasattr(result, name)
    }

    def serializable(value: Any) -> Any:
        if is_dataclass(value):
            return serializable(asdict(value))
        if isinstance(value, dict):
            return {str(key): serializable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [serializable(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return repr(value)

    return snapshot, content_sha256(serializable(snapshot))


def _restore_selection_guard(result: Any, snapshot: dict[str, Any]) -> None:
    if result is None:
        return
    for name, value in snapshot.items():
        setattr(result, name, copy.deepcopy(value))


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

    def create_session(self, *, profile_id: str = "competition_masld") -> AgentSession:
        return self.store.create(profile_id=profile_id)

    def get_session(self, session_id: str) -> AgentSession | None:
        return self.store.get(session_id)

    def attach_sdf(self, session: AgentSession, *, filename: str, content: bytes) -> None:
        session.sdf_bytes = content
        session.sdf_filename = filename or "library.sdf"
        session.sdf_ui_pending = True
        session.last_result = None
        session.last_run_id = ""
        session.last_selection_sha256 = ""
        session.last_molecule_index = {}
        session.last_mechanism_job_id = ""
        self.store.save_sdf(session)

    def detach_sdf(self, session: AgentSession) -> None:
        session.last_result = None
        session.last_run_id = ""
        session.last_selection_sha256 = ""
        session.last_config_hash = ""
        session.last_input_sha256 = ""
        session.last_molecule_index = {}
        session.last_mechanism_job_id = ""
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

    def settings_view(self, session: AgentSession | None = None) -> dict[str, Any]:
        installed = set(session.installed_catalog) if session else set()
        profile_id = session.profile_id if session else "competition_masld"
        return self.registry.settings_view(
            profile_id=profile_id,
            installed_catalog=installed,
        )

    def _emit(self, session: AgentSession, event: dict[str, Any]) -> dict[str, Any]:
        event.setdefault("claim_ceiling", claim_ceiling_default())
        return self.store.append_event(session, event)

    def handle_message(self, session: AgentSession, text: str) -> Iterator[dict[str, Any]]:
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

    def _handle_intent(
        self, session: AgentSession, text: str, intent: Any
    ) -> Iterator[dict[str, Any]]:
        session.top_n = intent.top_n
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

        def out(ev: dict[str, Any]) -> dict[str, Any]:
            return self._emit(session, ev)

        has_sdf = bool(session.sdf_bytes)
        has_result = session.last_result is not None

        # / @ 点选：单独介绍或试用，不联动整条筛选流水线
        if intent.mentions and intent.mention_action:
            yield from self._handle_mentions(session, intent)
            return

        # 自然语言证据查询是独立只读 Tool，不触发筛选、导出或 Catalog。
        if intent.query_evidence:
            yield from self._run_query_evidence(session, intent)
            yield out({"type": "done"})
            self.store.persist(session)
            return

        # 纯对话：用 LLM 回答（不强制 SDF）；失败再降级模板
        if not intent.wants_tools:
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
            reply = self._llm_chat_reply(session, text)
            session.messages.append({"role": "assistant", "text": reply})
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
                "skill_ids": list(intent.skill_ids),
                "raw_text": intent.raw_text,
                "limit_source": limit_src,
            }
            session.messages.append({"role": "assistant", "text": reply})
            yield out({"type": "assistant", "text": reply})
            yield out({"type": "done"})
            self.store.persist(session)
            return

        yield out(
            {
                "type": "thinking",
                "text": (
                    f"理解你的需求：{intent.reason}。"
                    f"将调用技能 {list(intent.skill_ids)}；"
                    "使用确定性科学工具，不会用模型直接改排名。"
                    + (f" 会话内可用附件：{session.sdf_filename}。" if has_sdf else " 尚未绑定 SDF 附件。")
                ),
            }
        )

        steps: list[str] = []
        if intent.want_csv or (intent.want_pdf and not has_result):
            steps.append(f"Skill masld_nominate：生成 Top{intent.top_n} 候选清单")
            steps.append("Tool export_nomination：导出 CSV")
        if intent.want_pdf:
            steps.append("Skill masld_mechanism：生成机制与验证方案 PDF")
        steps.append("返回可下载产物卡片")
        yield out({"type": "plan", "steps": steps})

        need_screen = intent.want_csv or (intent.want_pdf and not has_result)
        if need_screen and not has_sdf:
            yield out(
                {
                    "type": "assistant",
                    "text": (
                        "这个需求需要化合物库才能跑筛选。"
                        "请先在输入区上传 .sdf 附件，上传后直接再说一次你的需求"
                        f"（例如：生成 top{intent.top_n} 候选清单 csv"
                        + ("，并给出机制 pdf" if intent.want_pdf else "")
                        + "），我会自动调用对应技能与插件。"
                    ),
                }
            )
            yield out({"type": "done"})
            self.store.persist(session)
            return

        if need_screen:
            yield from self._run_nominate(session, top_n=intent.top_n)

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
            yield from self._run_mechanism(session)

        # Catalog enrichment：仅在已主动添加时执行；失败降级，不改主榜
        if session.installed_catalog and session.last_result is not None:
            yield from self._run_catalog_enrichment(session)

        yield out(
            {
                "type": "assistant",
                "text": format_run_completion(
                    want_csv=bool(intent.want_csv),
                    want_pdf=bool(intent.want_pdf),
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

    def _handle_mentions(self, session: AgentSession, intent) -> Iterator[dict[str, Any]]:
        action = intent.mention_action
        yield self._emit(
            session,
            {
                "type": "thinking",
                "text": (
                    f"识别到点选 {', '.join(m.raw for m in intent.mentions)}，"
                    f"按「{'试用' if action == 'invoke' else '介绍'}」单独处理，不联动其它步骤。"
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
                yield from self._export_nomination_only(session)
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
            yield from self._run_nominate(session, top_n=session.top_n)
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
                job_id = session.last_mechanism_job_id
                if not job_id:
                    yield self._emit(
                        session,
                        {
                            "type": "assistant",
                            "text": "尚无机制任务 id。可先试用 `@tool:start_mechanism_report`。",
                        },
                    )
                    return
                job = get_job(job_id)
                yield self._emit(
                    session,
                    {
                        "type": "assistant",
                        "text": (
                            f"机制任务 `{job_id}` 状态："
                            f"{getattr(job, 'status', job) if job else 'unknown'}。"
                        ),
                    },
                )
                return
            yield self._emit(
                session,
                {
                    "type": "plan",
                    "steps": [f"单独试用 {mention.raw}：生成机制 PDF"],
                },
            )
            yield from self._run_mechanism(session)
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
            yield self._emit(
                session,
                {
                    "type": "tool_start",
                    "tool": mid,
                    "plugin": info.get("plugin_id") or "",
                    "args": {"trial": True, "writes_selection": False},
                },
            )
            try:
                kwargs: dict[str, Any] = {}
                if mid.startswith("mcp_"):
                    kwargs["query"] = "trial"
                elif mid == "predict_pl_fitness":
                    kwargs["smiles_list"] = []
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
            session.messages.append({"role": "assistant", "text": reply})
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
        session.messages.append({"role": "assistant", "text": message})
        yield self._emit(session, {"type": "assistant", "text": message})

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
        csv_text = result.to_csv_text()
        csv_name = f"nomination_top{result.output_count}_{result.run_id[:8]}_trial.csv"
        art = Artifact(
            artifact_id=_aid(),
            kind="csv",
            filename=csv_name,
            title=f"Top{result.output_count} 候选分子清单",
            subtitle=f"单独试用 export_nomination · {csv_name}",
            media_type="text/csv; charset=utf-8",
            content=csv_text.encode("utf-8"),
        )
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
        yield self._emit(
            session,
            {
                "type": "card",
                "card": {
                    "kind": "csv",
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
            hist_lines: list[str] = []
            for m in session.messages[-8:-1]:
                role = m.get("role")
                body = (m.get("text") or "").strip()
                if role in {"user", "assistant"} and body:
                    hist_lines.append(f"{role}: {body[:400]}")
            history = "\n".join(hist_lines) if hist_lines else "（无）"

            system = (
                "你是 MolMind Agent，面向 MASLD 低毒降脂分子筛选的能力助手。"
                "用简洁中文直接回答用户问题（化学概念、用法、流程等）。"
                "不要编造具体筛选排名或虚构实验数据。"
                "若用户其实想导出候选 CSV / 机制 PDF，可提示他们用自然语言描述产物；"
                "不要只回复『不调用工具』这类空话。"
                f"当前会话附件：{'已绑定 ' + name if has_sdf else '无'}（仅本会话可用，不跨会话）。"
            )
            user = (
                f"最近对话：\n{history}\n\n"
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

    def _run_nominate(self, session: AgentSession, *, top_n: int) -> Iterator[dict[str, Any]]:
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
                "text": "正在解析 SDF，并执行硬过滤 → 降脂/毒性打分 → 排序与多样性约束…",
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

        for ev in log_events[-12:]:
            yield self._emit(session, ev)

        session.last_result = result
        session.last_run_id = result.run_id
        session.last_selection_sha256 = result.selection_sha256
        session.last_config_hash = result.config.config_hash
        session.last_input_sha256 = result.input_sha256
        session.last_molecule_index = _molecule_index_from_result(result)

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

        yield self._emit(
            session,
            {
                "type": "tool_start",
                "tool": "export_nomination",
                "plugin": "molmind-core",
                "args": {"format": "csv", "top_n": top_n},
            },
        )
        csv_text = result.to_csv_text()
        csv_name = f"nomination_top{result.output_count}_{result.run_id[:8]}.csv"
        art = Artifact(
            artifact_id=_aid(),
            kind="csv",
            filename=csv_name,
            title=f"Top{result.output_count} 候选分子清单",
            subtitle=f"{csv_name} · selection_sha256={result.selection_sha256[:12]}…",
            media_type="text/csv; charset=utf-8",
            content=csv_text.encode("utf-8"),
        )
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
        yield self._emit(
            session,
            {
                "type": "card",
                "card": {
                    "kind": "csv",
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
        yield self._emit(
            session,
            {
                "type": "tool_end",
                "tool": "start_mechanism_report",
                "ok": True,
                "digest": {"job_id": job_id},
            },
        )

        deadline = time.time() + 180.0
        last_status = ""
        while time.time() < deadline:
            job = get_job(job_id)
            if not job:
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
                yield self._emit(
                    session,
                    {
                        "type": "error",
                        "detail": f"机制 PDF 生成失败：{job.get('error') or 'unknown'}",
                    },
                )
                return
            time.sleep(1.0)

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
