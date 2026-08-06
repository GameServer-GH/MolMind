"""Registry-driven task routing for plugin capabilities.

Phase 2 keeps routing deterministic and auditable while moving domain terms,
Skill ownership and Tool selection out of the Agent Loop. A constrained LLM
planner can replace capability matching in Phase 3 without changing the Loop
contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any


@dataclass(frozen=True)
class TaskRoute:
    route: str
    capability_id: str
    skill_id: str
    tool_id: str
    label: str
    arguments: dict[str, Any]
    reason: str
    evidence_required: bool = True
    planner_status: str = "deterministic"
    confidence: float = 1.0


class TaskRouter:
    def __init__(self, registry: Any) -> None:
        self.registry = registry

    def route(self, intent: Any, session: Any) -> TaskRoute:
        """Unified lane selection from structured Intent + Session state.

        Priority: deny → explain → evidence → core → scp → chat/clarify.
        Domain terms stay in Intent / capability YAML; this method only
        consumes structured flags and Session freeze facts.
        """
        from agent.memory.frozen_ranking import ensure_session_last_result, has_durable_freeze
        from agent.runtime.governance import frozen_ranking_mutation_requested

        text = str(getattr(intent, "raw_text", "") or "")
        enabled = {
            str(skill_id)
            for skill_id, meta in (getattr(session, "installed_scp_skills", None) or {}).items()
            if isinstance(meta, dict) and meta.get("enabled", True)
        }

        # Execution-gate block must not fall through to SCP capability matching
        # (e.g. 「Top20」misread as literature top_k after a deferred Core turn).
        turn_gate = str(getattr(session, "turn_execution_gate", "") or "").strip().lower()
        if turn_gate == "block":
            dialog_act = str(
                getattr(session, "turn_execution_dialog_act", "") or "discuss_only"
            ).strip().lower() or "discuss_only"
            if dialog_act == "clarify":
                return TaskRoute(
                    route="clarify",
                    capability_id="",
                    skill_id="",
                    tool_id="",
                    label="确认是否执行",
                    arguments={},
                    reason="execution_gate_block:clarify",
                    evidence_required=False,
                    planner_status="deterministic",
                    confidence=1.0,
                )
            return TaskRoute(
                route="chat",
                capability_id="",
                skill_id="",
                tool_id="",
                label="本轮不执行工具",
                arguments={},
                reason=f"execution_gate_block:{dialog_act}",
                evidence_required=False,
                planner_status="deterministic",
                confidence=1.0,
            )

        if frozen_ranking_mutation_requested(text):
            return TaskRoute(
                route="deny",
                capability_id="",
                skill_id="",
                tool_id="",
                label="拒绝改写冻结排名",
                arguments={},
                reason="frozen_ranking_boundary_scp_cannot_rewrite_selection",
                evidence_required=False,
                planner_status="deterministic",
                confidence=1.0,
            )

        ensure_session_last_result(session)
        has_freeze = has_durable_freeze(session) or getattr(session, "last_result", None) is not None

        if getattr(intent, "explain_ranking", False):
            if has_freeze:
                return TaskRoute(
                    route="explain",
                    capability_id="ranking_explain",
                    skill_id="masld_explain",
                    tool_id="",
                    label="解释冻结排名",
                    arguments={
                        "molecule_id": getattr(intent, "ranking_molecule_id", None),
                        "rank_positions": list(getattr(intent, "ranking_positions", ()) or ()),
                        "rank_position_subject": bool(
                            getattr(intent, "ranking_position_subject", False)
                        ),
                    },
                    reason="structured_ranking_followup_with_freeze",
                    evidence_required=False,
                    planner_status="deterministic",
                    confidence=1.0,
                )
            return TaskRoute(
                route="clarify",
                capability_id="ranking_explain",
                skill_id="",
                tool_id="",
                label="缺少冻结结果",
                arguments={},
                reason="ranking_followup_missing_frozen_result",
                evidence_required=False,
                planner_status="deterministic",
                confidence=1.0,
            )

        if getattr(intent, "query_evidence", False):
            return TaskRoute(
                route="evidence",
                capability_id="molecule_evidence",
                skill_id="masld_explain",
                tool_id="query_evidence",
                label="分子证据卡",
                arguments={},
                reason="structured_core_evidence_query",
                evidence_required=True,
                planner_status="deterministic",
                confidence=1.0,
            )

        if getattr(intent, "mentions", None) and getattr(intent, "mention_action", ""):
            return TaskRoute(
                route="core",
                capability_id="mention",
                skill_id="",
                tool_id="",
                label="点选插件或技能",
                arguments={},
                reason="structured_mention_action",
                evidence_required=False,
                planner_status="deterministic",
                confidence=1.0,
            )

        core_deliverable = bool(
            getattr(intent, "wants_tools", False)
            and (
                getattr(intent, "want_csv", False)
                or getattr(intent, "want_pdf", False)
                or getattr(intent, "want_reserve", False)
                or getattr(intent, "want_bundle", False)
                or getattr(intent, "execution_requested", False)
            )
        )
        if core_deliverable:
            return TaskRoute(
                route="core",
                capability_id="masld_nomination",
                skill_id=(getattr(intent, "skill_ids", ()) or ("masld_nominate",))[0],
                tool_id="score_and_rank",
                label="Core 筛选/导出",
                arguments={"top_n": int(getattr(intent, "top_n", 10) or 10)},
                reason="structured_core_deliverable",
                evidence_required=False,
                planner_status="deterministic",
                confidence=1.0,
            )

        plugin = self.registry.plugins.get("scp-hub")
        declared_skill_ids = {
            str(capability.get("skill_id") or "")
            for capability in (getattr(plugin, "capabilities", None) or [])
            if isinstance(capability, dict) and capability.get("skill_id")
        }
        recent = list(getattr(session, "messages", None) or [])[-6:]

        # Session act first (inventory / propose_install / chat / deny). Do not
        # let YAML task_terms short-circuit meta questions ahead of this decision.
        dialog = self.plan_session_act(
            text,
            session,
            recent_messages=recent,
        )
        if dialog is not None:
            if dialog.route == "deny":
                return dialog
            if dialog.route == "clarify" and str(dialog.reason or "").startswith(
                "scp_skill_not_installed:"
            ):
                return dialog
            if dialog.route == "chat" and "inventory" in str(dialog.reason or ""):
                return dialog

        # Full-catalog SCP planning (enabled + installable). Call even when no
        # skill is enabled so the planner can still propose_install.
        if declared_skill_ids:
            scp = self.plan_scp(
                text,
                enabled_skill_ids=enabled,
                recent_messages=recent,
            )
            if scp is not None and str(scp.route) == "scp":
                return scp
            if scp is not None and str(scp.route) in {"clarify", "deny"}:
                return scp
            if scp is not None and str(scp.route) == "chat":
                reason = str(scp.reason or "")
                if "inventory" in reason or dialog is None:
                    return scp

        # LLM-down only: task_terms may propose install / execute when session-act
        # LLM was unavailable. Never use this path to override a live inventory act.
        if dialog is None and declared_skill_ids:
            declared = self.route_scp(text, enabled_skill_ids=declared_skill_ids)
            if (
                declared is not None
                and str(declared.skill_id or "")
                and str(declared.skill_id) not in enabled
            ):
                return TaskRoute(
                    route="clarify",
                    capability_id=str(declared.capability_id or ""),
                    skill_id=str(declared.skill_id or ""),
                    tool_id="",
                    label=str(declared.label or "科研能力"),
                    arguments={},
                    reason=f"scp_skill_not_installed:{declared.skill_id}",
                    evidence_required=True,
                    planner_status="deterministic_offline",
                    confidence=1.0,
                )
            if enabled:
                executed = self.route_scp(text, enabled_skill_ids=enabled)
                if executed is not None:
                    return executed

        if dialog is not None:
            return dialog

        return TaskRoute(
            route="chat",
            capability_id="",
            skill_id="",
            tool_id="",
            label="普通对话",
            arguments={},
            reason="no_structured_tool_lane",
            evidence_required=False,
            planner_status="deterministic",
            confidence=1.0,
        )

    def route_scp(self, text: str, *, enabled_skill_ids: set[str]) -> TaskRoute | None:
        plugin = self.registry.plugins.get("scp-hub")
        if plugin is None:
            return None
        raw = str(text or "").strip()
        low = raw.lower()
        candidates: list[tuple[int, int, dict[str, Any]]] = []
        for order, capability in enumerate(plugin.capabilities or []):
            if not isinstance(capability, dict):
                continue
            skill_id = str(capability.get("skill_id") or "")
            if not skill_id or skill_id not in enabled_skill_ids:
                continue
            terms = [str(term).lower() for term in capability.get("task_terms") or [] if str(term)]
            excluded = [str(term).lower() for term in capability.get("exclude_terms") or [] if str(term)]
            if excluded and any(term in low for term in excluded):
                continue
            score = sum(1 for term in terms if term in low)
            if score:
                candidates.append((score, -order, capability))
        if not candidates:
            return None
        capability = max(candidates, key=lambda item: (item[0], item[1]))[2]
        return self._route_from_capability(capability, raw, plugin.terminology or {})

    def route_scp_tasks(
        self, text: str, *, enabled_skill_ids: set[str]
    ) -> list[TaskRoute]:
        """Return every explicitly requested capability in declared order."""
        plugin = self.registry.plugins.get("scp-hub")
        if plugin is None:
            return []
        raw = str(text or "").strip()
        low = raw.lower()
        matched: list[tuple[int, int, dict[str, Any]]] = []
        for order, capability in enumerate(plugin.capabilities or []):
            if not isinstance(capability, dict):
                continue
            skill_id = str(capability.get("skill_id") or "")
            if skill_id not in enabled_skill_ids:
                continue
            terms = [
                str(value).lower()
                for value in (
                    capability.get("multi_task_terms")
                    or capability.get("task_terms")
                    or []
                )
                if str(value)
            ]
            score = sum(value in low for value in terms)
            if score:
                matched.append(
                    (
                        int(capability.get("execution_order") or 100),
                        order,
                        capability,
                    )
                )
        if len(matched) < 2:
            return []
        routes = [
            self._route_from_capability(capability, raw, plugin.terminology or {})
            for _, _, capability in sorted(matched, key=lambda value: (value[0], value[1]))
        ]
        return [route for route in routes if route is not None]

    def evidence_dependencies(self, capability_id: str) -> list[str]:
        plugin = self.registry.plugins.get("scp-hub")
        capability = next(
            (
                item
                for item in (getattr(plugin, "capabilities", None) or [])
                if isinstance(item, dict)
                and str(item.get("capability_id") or "") == capability_id
            ),
            {},
        )
        return [str(value) for value in capability.get("evidence_dependencies") or []]

    def claim_scopes(self, capability_id: str) -> list[str]:
        plugin = self.registry.plugins.get("scp-hub")
        capability = next(
            (
                item
                for item in (getattr(plugin, "capabilities", None) or [])
                if isinstance(item, dict)
                and str(item.get("capability_id") or "") == capability_id
            ),
            {},
        )
        return [str(value) for value in capability.get("claim_scopes") or []]

    def route_capability(
        self,
        capability_id: str,
        text: str,
        *,
        enabled_skill_ids: set[str],
    ) -> TaskRoute | None:
        """Build one plugin-owned capability route without term matching."""
        plugin = self.registry.plugins.get("scp-hub")
        if plugin is None:
            return None
        capability = next(
            (
                item
                for item in plugin.capabilities or []
                if isinstance(item, dict)
                and str(item.get("capability_id") or "") == capability_id
                and str(item.get("skill_id") or "") in enabled_skill_ids
            ),
            None,
        )
        if capability is None:
            return None
        return self._route_from_capability(
            capability, str(text or "").strip(), plugin.terminology or {}
        )

    def recovery_steps(
        self,
        capability_id: str,
        text: str,
        *,
        enabled_skill_ids: set[str],
    ) -> tuple[list[dict[str, Any]], TaskRoute | None]:
        """Resolve declarative recovery calls and an optional fallback route."""
        plugin = self.registry.plugins.get("scp-hub")
        capability = next(
            (
                item
                for item in (getattr(plugin, "capabilities", None) or [])
                if isinstance(item, dict)
                and str(item.get("capability_id") or "") == capability_id
                and str(item.get("skill_id") or "") in enabled_skill_ids
            ),
            None,
        )
        if capability is None:
            return [], None
        recovery = capability.get("recovery") or {}
        terminology = getattr(plugin, "terminology", {}) or {}
        resolved: list[dict[str, Any]] = []
        max_calls = max(0, int(recovery.get("max_calls") or 0))
        for step in (recovery.get("steps") or [])[:max_calls]:
            if not isinstance(step, dict):
                continue
            tool_id = str(step.get("tool_id") or "")
            tool = self.registry.tools.get(tool_id)
            if not tool or tool.writes_selection:
                continue
            builder = str(step.get("argument_builder") or "")
            arguments = (
                self._build_arguments(
                    builder,
                    str(text or ""),
                    capability=capability,
                    terminology=terminology,
                )
                if builder
                else dict(step.get("arguments") or {})
            )
            if not self._arguments_valid(arguments, tool.input_schema):
                continue
            resolved.append(
                {
                    "tool_id": tool_id,
                    "title": str(step.get("title") or tool.title or tool_id),
                    "evidence_role": str(step.get("evidence_role") or "evidence_query"),
                    "arguments": arguments,
                }
            )
        fallback_id = str(recovery.get("fallback_capability_id") or "")
        fallback = (
            self.route_capability(
                fallback_id,
                text,
                enabled_skill_ids=enabled_skill_ids,
            )
            if fallback_id
            else None
        )
        fallback_builder = str(recovery.get("fallback_argument_builder") or "")
        if fallback is not None and fallback_builder:
            fallback_capability = next(
                (
                    item
                    for item in (getattr(plugin, "capabilities", None) or [])
                    if isinstance(item, dict)
                    and str(item.get("capability_id") or "")
                    == fallback.capability_id
                ),
                {},
            )
            fallback = TaskRoute(
                route=fallback.route,
                capability_id=fallback.capability_id,
                skill_id=fallback.skill_id,
                tool_id=fallback.tool_id,
                label=fallback.label,
                arguments=self._build_arguments(
                    fallback_builder,
                    str(text or ""),
                    capability=fallback_capability,
                    terminology=terminology,
                ),
                reason="plugin_recovery_fallback",
                evidence_required=fallback.evidence_required,
                planner_status="deterministic_recovery",
                confidence=1.0,
            )
        return resolved, fallback

    def _route_from_capability(
        self,
        capability: dict[str, Any],
        raw: str,
        terminology: dict[str, Any],
    ) -> TaskRoute | None:
        skill_id = str(capability.get("skill_id") or "")
        tools = [str(tool) for tool in capability.get("tools") or [] if str(tool)]
        if not tools:
            return None
        tool_id = tools[0]
        arguments = self._build_arguments(
            str(capability.get("argument_builder") or "passthrough"),
            raw,
            capability=capability,
            terminology=terminology,
        )
        return TaskRoute(
            route="scp",
            capability_id=str(capability.get("capability_id") or ""),
            skill_id=skill_id,
            tool_id=tool_id,
            label=str(capability.get("title") or capability.get("capability_id") or skill_id),
            arguments=arguments,
            reason="registry_capability_match",
            evidence_required=bool(capability.get("evidence_required", True)),
        )

    def plan_session_act(
        self,
        text: str,
        session: Any,
        *,
        recent_messages: list[dict[str, Any]] | None = None,
    ) -> TaskRoute | None:
        """LLM dialog decision over the live capability surface (no keyword tables)."""
        try:
            from agent.runtime.capability_context import (
                build_capability_surface,
                format_capability_surface_for_prompt,
            )
            from plugins.molmind_core.scientific.mechanism.llm_client import (
                chat_completion,
                resolve_llm_settings,
            )

            settings = resolve_llm_settings(
                {"enabled": True, "agent_chat": True}, purpose="agent_chat"
            )
            if not settings.ready:
                return None
            settings = type(settings)(
                enabled=settings.enabled,
                model=settings.model,
                base_url=settings.base_url,
                api_key=settings.api_key,
                temperature=0.0,
                timeout_sec=min(settings.timeout_sec, 12.0),
                max_tokens=min(max(settings.max_tokens, 320), 700),
                cache_dir=settings.cache_dir,
                use_cache=False,
            )
            surface = build_capability_surface(self.registry, session)
            installable_ids = {
                str(item.get("skill_id") or "")
                for item in surface.get("installable_scp_skills") or []
                if str(item.get("skill_id") or "") and not bool(item.get("enabled"))
            }
            capability_by_skill = {
                str(item.get("skill_id") or ""): str(item.get("capability_id") or "")
                for item in surface.get("installable_scp_skills") or []
                if str(item.get("skill_id") or "")
            }
            title_by_skill = {
                str(item.get("skill_id") or ""): str(
                    item.get("title") or item.get("skill_id") or ""
                )
                for item in surface.get("installable_scp_skills") or []
                if str(item.get("skill_id") or "")
            }
            history = "\n".join(
                f"{item.get('role')}: {str(item.get('text') or '')[:280]}"
                for item in (recent_messages or [])[-4:]
                if item.get("role") in {"user", "assistant"}
            )
            system = (
                "你是 MolMind 的会话决策器。只返回 JSON，不要 Markdown。"
                "格式：{\"act\":\"inventory|propose_install|chat|deny\","
                "\"skill_id\":\"\",\"confidence\":0.0,\"reason\":\"...\"}。"
                "依据下方「能力面」事实做决策，禁止编造未列出的插件/技能/工具。"
                "act 含义："
                "inventory=用户在询问当前已装或可装的插件/技能/工具/MCP/Catalog，"
                "应基于能力面回答，不执行科研工具；"
                "propose_install=用户意图明显需要某个尚未启用的 SCP skill，"
                "skill_id 必须来自能力面中未启用的 installable_scp_skills；"
                "chat=普通概念、用法、流程或闲聊；"
                "deny=用户想用实时资料改写/重算冻结主榜。"
                "根据用户目标语义判断，不要臆造能力。"
            )
            user = (
                f"能力面：{format_capability_surface_for_prompt(surface)}\n"
                f"最近对话：{history or '（无）'}\n"
                f"用户本轮：{text}"
            )
            raw = chat_completion(settings, system=system, user=user).strip()
            match = re.search(r"\{[\s\S]*\}", raw)
            data = json.loads(match.group(0) if match else raw)
            if not isinstance(data, dict):
                return None
            act = str(data.get("act") or "chat").strip().lower()
            skill_id = str(data.get("skill_id") or "").strip()
            reason = str(data.get("reason") or "session_act")[:500]
            confidence = self._confidence(data.get("confidence"))
            if act == "deny":
                return TaskRoute(
                    route="deny",
                    capability_id="",
                    skill_id="",
                    tool_id="",
                    label="拒绝改写冻结排名",
                    arguments={},
                    reason=f"session_act_deny:{reason}",
                    evidence_required=False,
                    planner_status="llm",
                    confidence=confidence,
                )
            if act == "propose_install" and skill_id and skill_id in installable_ids:
                return TaskRoute(
                    route="clarify",
                    capability_id=capability_by_skill.get(skill_id, ""),
                    skill_id=skill_id,
                    tool_id="",
                    label=title_by_skill.get(skill_id, skill_id),
                    arguments={},
                    reason=f"scp_skill_not_installed:{skill_id}",
                    evidence_required=True,
                    planner_status="llm",
                    confidence=confidence,
                )
            if act == "inventory":
                return TaskRoute(
                    route="chat",
                    capability_id="",
                    skill_id="",
                    tool_id="",
                    label="能力清单说明",
                    arguments={},
                    reason=f"session_act_inventory:{reason}",
                    evidence_required=False,
                    planner_status="llm",
                    confidence=confidence,
                )
            return TaskRoute(
                route="chat",
                capability_id="",
                skill_id="",
                tool_id="",
                label="普通对话",
                arguments={},
                reason=f"session_act_chat:{reason}",
                evidence_required=False,
                planner_status="llm",
                confidence=confidence,
            )
        except Exception:
            return None

    def plan_scp(
        self,
        text: str,
        *,
        enabled_skill_ids: set[str],
        recent_messages: list[dict[str, Any]] | None = None,
        allow_unregistered: bool = False,
    ) -> TaskRoute | None:
        """Return a constrained LLM plan or the deterministic Phase-2 route."""
        fallback = self.route_scp(text, enabled_skill_ids=enabled_skill_ids)
        plugin = self.registry.plugins.get("scp-hub")
        declared = [
            capability
            for capability in (getattr(plugin, "capabilities", None) or [])
            if isinstance(capability, dict) and str(capability.get("skill_id") or "")
        ]
        enabled_caps = [
            capability
            for capability in declared
            if str(capability.get("skill_id") or "") in enabled_skill_ids
        ]
        installable_caps = [
            capability
            for capability in declared
            if str(capability.get("skill_id") or "") not in enabled_skill_ids
        ]
        # Keep the full enabled catalog for the LLM. task_terms (fallback) is only
        # a soft retrieval_hint and the offline/invalid-plan fallback — never a
        # hard candidate narrow that overrides planner choice among enabled caps.
        candidates = enabled_caps
        hint_capability_id = str(fallback.capability_id or "") if fallback is not None else ""
        if not candidates and not installable_caps:
            return fallback
        try:
            from plugins.molmind_core.scientific.mechanism.llm_client import (
                chat_completion,
                resolve_llm_settings,
            )

            settings = resolve_llm_settings(
                {"enabled": True, "agent_chat": True}, purpose="agent_chat"
            )
            if not settings.ready:
                return fallback
            settings = type(settings)(
                enabled=settings.enabled,
                model=settings.model,
                base_url=settings.base_url,
                api_key=settings.api_key,
                temperature=0.0,
                timeout_sec=min(settings.timeout_sec, 12.0),
                max_tokens=min(max(settings.max_tokens, 400), 900),
                cache_dir=settings.cache_dir,
                use_cache=False,
            )

            def _cap_entry(capability: dict[str, Any], *, status: str) -> dict[str, Any]:
                tools = []
                for tool_id in capability.get("tools") or []:
                    tool = self.registry.tools.get(str(tool_id))
                    tools.append(
                        {
                            "tool_id": str(tool_id),
                            "input_schema": getattr(tool, "input_schema", {}) if tool else {},
                            "writes_selection": bool(getattr(tool, "writes_selection", False))
                            if tool
                            else False,
                        }
                    )
                entry = {
                    "status": status,
                    "capability_id": capability.get("capability_id"),
                    "skill_id": capability.get("skill_id"),
                    "title": capability.get("title"),
                    "domains": capability.get("domains") or [],
                    "entity_types": capability.get("entity_types") or [],
                    "supports": capability.get("supports") or [],
                    "output_types": capability.get("output_types") or [],
                    "default_arguments": capability.get("default_arguments") or {},
                    "planner_argument_keys": capability.get("planner_argument_keys") or [],
                    "tools": tools,
                }
                if (
                    status == "enabled"
                    and hint_capability_id
                    and str(capability.get("capability_id") or "") == hint_capability_id
                ):
                    entry["retrieval_hint"] = True
                return entry

            catalog = [
                *[_cap_entry(item, status="enabled") for item in candidates],
                *[_cap_entry(item, status="installable") for item in installable_caps],
            ]
            history = "\n".join(
                f"{item.get('role')}: {str(item.get('text') or '')[:300]}"
                for item in (recent_messages or [])[-4:]
                if item.get("role") in {"user", "assistant"}
            )
            system = (
                "你是 MolMind 的受约束 Plugin Capability Planner。只返回 JSON，不要 Markdown。"
                "格式：{\"route\":\"scp|propose_install|inventory|chat|clarify|deny\","
                "\"capability_id\":\"...\",\"skill_id\":\"...\",\"tool_id\":\"...\","
                "\"arguments\":{},\"confidence\":0.0,\"reason\":\"...\"}。"
                "能力目录含 status=enabled|installable。"
                "route=scp 只能选择 status=enabled 且 writes_selection=false 的能力与工具；"
                "arguments 必须满足 input_schema。"
                "retrieval_hint=true 仅作检索提示，不是强制；你仍可在 enabled 中另选更合适的能力。"
                "route=propose_install：用户意图需要 status=installable 的 skill，"
                "skill_id/capability_id 必须来自该条目。"
                "route=inventory：用户在询问已装/可装能力清单，不执行工具。"
                "实时资料只能作 supplementary evidence，不能重算或修改候选排名。"
                "default_arguments 由插件固定，planner_argument_keys 之外的参数不会被采纳。"
                "普通对话返回 chat；缺少用户必须补充的信息时才返回 clarify；"
                "请求用实时资料改榜时返回 deny。"
            )
            user = (
                f"能力目录：{json.dumps(catalog, ensure_ascii=False)}\n"
                f"最近对话：{history or '（无）'}\n用户本轮：{text}"
            )
            raw = chat_completion(settings, system=system, user=user).strip()
            match = re.search(r"\{[\s\S]*\}", raw)
            data = json.loads(match.group(0) if match else raw)
            route_name = str(data.get("route") or "")
            if route_name == "inventory":
                return TaskRoute(
                    route="chat",
                    capability_id="",
                    skill_id="",
                    tool_id="",
                    label="能力清单说明",
                    arguments={},
                    reason=str(data.get("reason") or "planner_inventory")[:500],
                    evidence_required=False,
                    planner_status="llm",
                    confidence=self._confidence(data.get("confidence")),
                )
            if route_name == "propose_install":
                skill_id = str(data.get("skill_id") or "").strip()
                capability = next(
                    (
                        item
                        for item in installable_caps
                        if str(item.get("skill_id") or "") == skill_id
                    ),
                    None,
                )
                if capability is None:
                    return fallback
                return TaskRoute(
                    route="clarify",
                    capability_id=str(capability.get("capability_id") or ""),
                    skill_id=skill_id,
                    tool_id="",
                    label=str(capability.get("title") or skill_id),
                    arguments={},
                    reason=f"scp_skill_not_installed:{skill_id}",
                    evidence_required=True,
                    planner_status="llm",
                    confidence=self._confidence(data.get("confidence")),
                )
            if route_name != "scp":
                declined_route = route_name or "chat"
                if declined_route not in {"chat", "clarify", "deny"}:
                    return fallback
                return TaskRoute(
                    route=declined_route,
                    capability_id="",
                    skill_id="",
                    tool_id="",
                    label=(
                        "治理拒绝"
                        if declined_route == "deny"
                        else "需要澄清" if declined_route == "clarify" else "普通对话"
                    ),
                    arguments={},
                    reason=str(data.get("reason") or "planner_declined")[:500],
                    evidence_required=True,
                    planner_status="llm",
                    confidence=self._confidence(data.get("confidence")),
                )
            capability_id = str(data.get("capability_id") or "")
            capability = next(
                (item for item in candidates if str(item.get("capability_id") or "") == capability_id),
                None,
            )
            if capability is None:
                return fallback
            skill_id = str(data.get("skill_id") or "")
            tool_id = str(data.get("tool_id") or "")
            allowed_tools = [str(value) for value in capability.get("tools") or []]
            tool = self.registry.tools.get(tool_id)
            if (
                skill_id != str(capability.get("skill_id") or "")
                or skill_id not in enabled_skill_ids
                or tool_id not in allowed_tools
                or (tool is None and not allow_unregistered)
                or (tool is not None and tool.writes_selection)
            ):
                return fallback
            if tool is None and allow_unregistered:
                return TaskRoute(
                    route="scp",
                    capability_id=capability_id,
                    skill_id=skill_id,
                    tool_id=tool_id,
                    label=str(capability.get("title") or capability_id),
                    arguments=self._build_arguments(
                        str(capability.get("argument_builder") or "passthrough"),
                        str(text or ""),
                        capability=capability,
                        terminology=getattr(plugin, "terminology", {}) or {},
                    ),
                    reason=str(data.get("reason") or "llm_capability_plan")[:500],
                    evidence_required=bool(capability.get("evidence_required", True)),
                    planner_status="llm_preflight",
                    confidence=self._confidence(data.get("confidence")),
                )
            arguments = self._normalize_arguments(
                data.get("arguments"),
                capability=capability,
                tool=tool,
                text=str(text or ""),
                terminology=getattr(plugin, "terminology", {}) or {},
            )
            if arguments is None:
                return fallback
            return TaskRoute(
                route="scp",
                capability_id=capability_id,
                skill_id=skill_id,
                tool_id=tool_id,
                label=str(capability.get("title") or capability_id),
                arguments=arguments,
                reason=str(data.get("reason") or "llm_capability_plan")[:500],
                evidence_required=bool(capability.get("evidence_required", True)),
                planner_status="llm",
                confidence=self._confidence(data.get("confidence")),
            )
        except Exception:
            return fallback

    @staticmethod
    def _confidence(value: Any) -> float:
        try:
            return min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _arguments_valid(arguments: dict[str, Any], schema: dict[str, Any]) -> bool:
        if not isinstance(schema, dict) or not schema:
            return True
        properties = schema.get("properties") or {}
        required = [str(value) for value in schema.get("required") or []]
        if any(key not in arguments for key in required):
            return False
        if schema.get("additionalProperties") is False and any(
            key not in properties for key in arguments
        ):
            return False
        type_map = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        for key, value in arguments.items():
            spec = properties.get(key)
            if not isinstance(spec, dict):
                continue
            expected = spec.get("type")
            if expected in type_map and not isinstance(value, type_map[expected]):
                return False
        return True

    def _normalize_arguments(
        self,
        arguments: Any,
        *,
        capability: dict[str, Any],
        tool: Any,
        text: str,
        terminology: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Apply plugin-owned defaults and restrict planner-controlled fields.

        The planner may supply query content, but cannot invent remote routing
        parameters such as an unsupported subject or knowledge-graph name.
        Those values remain declared by the plugin capability.
        """
        if not isinstance(arguments, dict):
            return None
        builder = str(capability.get("argument_builder") or "")
        # Seed from the plugin builder so required scientific/safety arguments
        # exist even when the LLM omits them.
        normalized = self._build_arguments(
            builder,
            text,
            capability=capability,
            terminology=terminology,
        )
        allowed = capability.get("planner_argument_keys")
        if not isinstance(allowed, list):
            allowed = {
                "literature_query": ["query"],
                "mechanism_cypher": ["cypher"],
                "user_prompt": ["user_prompt"],
            }.get(builder, list(arguments))
        for key in allowed:
            if str(key) in arguments:
                normalized[str(key)] = arguments[str(key)]
        # The plugin builder has already normalized the user text. Re-apply
        # terminology expansion only when an accepted planner-owned query
        # actually replaced it; otherwise the same constraints are appended
        # twice and equivalent routes produce different cache keys.
        if (
            builder == "literature_query"
            and "query" in allowed
            and "query" in arguments
            and isinstance(normalized.get("query"), str)
        ):
            normalized["query"] = self._literature_query(
                normalized["query"], terminology, capability=capability
            )
        if not self._arguments_valid(normalized, getattr(tool, "input_schema", {})):
            return None
        return normalized

    def _build_arguments(
        self,
        builder: str,
        text: str,
        *,
        capability: dict[str, Any],
        terminology: dict[str, Any],
    ) -> dict[str, Any]:
        defaults = dict(capability.get("default_arguments") or {})
        if builder == "literature_query":
            defaults["query"] = self._literature_query(
                text, terminology, capability=capability
            )
        elif builder == "mechanism_cypher":
            defaults["cypher"] = self._mechanism_cypher(text, terminology)
        elif builder == "mechanism_target_neighbors":
            defaults["cypher"] = self._mechanism_target_neighbors(text, terminology)
        elif builder == "mechanism_context_paths":
            defaults["cypher"] = self._mechanism_context_paths(text, terminology)
        elif builder == "mechanism_literature_query":
            defaults["query"] = self._mechanism_literature_query(
                text, terminology, capability=capability
            )
        elif builder == "user_prompt":
            defaults["user_prompt"] = text
        else:
            defaults.setdefault("query", text)
        return defaults

    @staticmethod
    def _matched_aliases(text: str, terminology: dict[str, Any]) -> dict[str, list[str]]:
        low = text.lower()
        matched: dict[str, list[str]] = {}
        for entity_type, canonical_map in terminology.items():
            if not isinstance(canonical_map, dict):
                continue
            for canonical, aliases in canonical_map.items():
                values = [str(canonical), *[str(alias) for alias in aliases or []]]
                if any(value.lower() in low for value in values if value):
                    matched.setdefault(str(entity_type), []).extend(values)
        return matched

    def _literature_query(
        self,
        text: str,
        terminology: dict[str, Any],
        *,
        capability: dict[str, Any] | None = None,
    ) -> str:
        matched = self._matched_aliases(text, terminology)
        excluded = self._excluded_aliases(text, terminology)
        low = str(text or "").lower()
        incompatible = {
            str(value)
            for value in (capability or {}).get("incompatible_concepts") or []
        }
        for canonical_map in terminology.values():
            if not isinstance(canonical_map, dict):
                continue
            for canonical, raw_aliases in canonical_map.items():
                if str(canonical) not in incompatible:
                    continue
                values = [str(canonical), *[str(value) for value in raw_aliases or []]]
                if not any(value.lower() in low for value in values if value):
                    excluded.update(value.lower() for value in values if value)
        anchors = list(
            dict.fromkeys(
                value
                for values in matched.values()
                for value in values
                if value.lower() not in excluded
            )
        )
        if not anchors:
            return text
        suffix = f"\nScientific domain constraints: {'; '.join(anchors)}"
        if excluded:
            suffix += f"\nScientific exclusions: {'; '.join(sorted(excluded))}"
        return f"{text}{suffix}"

    @staticmethod
    def _excluded_aliases(text: str, terminology: dict[str, Any]) -> set[str]:
        clauses = re.findall(
            r"(?:排除|剔除|不包括|不要|exclude|excluding|without)\s*([^。；;\n]+)",
            str(text or ""),
            flags=re.IGNORECASE,
        )
        excluded_text = " ".join(clauses).lower()
        aliases: set[str] = set()
        if not excluded_text:
            return aliases
        for canonical_map in terminology.values():
            if not isinstance(canonical_map, dict):
                continue
            for canonical, raw_aliases in canonical_map.items():
                values = [str(canonical), *[str(value) for value in raw_aliases or []]]
                if any(
                    TaskRouter._positive_alias_mention(excluded_text, value)
                    for value in values
                    if value
                ):
                    aliases.update(value.lower() for value in values if value)
        return aliases

    @staticmethod
    def _positive_alias_mention(text: str, alias: str) -> bool:
        for match in re.finditer(re.escape(str(alias).lower()), str(text).lower()):
            prefix = str(text).lower()[max(0, match.start() - 8) : match.start()]
            if re.search(r"(?:非|非-|non[-\s]?)$", prefix):
                continue
            return True
        return False

    def _mechanism_cypher(self, text: str, terminology: dict[str, Any]) -> str:
        matched = self._matched_aliases(text, terminology)
        target_terms = matched.get("target") or ["target"]
        context_terms = [
            value
            for entity_type, values in matched.items()
            if entity_type != "target"
            for value in values
        ] or ["disease", "pathway"]

        target_n = self._mechanism_match_expression("n", target_terms)
        target_m = self._mechanism_match_expression("m", target_terms)
        context_n = self._mechanism_match_expression("n", context_terms)
        context_m = self._mechanism_match_expression("m", context_terms)
        return (
            "MATCH (n)-[r]-(m) "
            f"WHERE (({target_n} AND {context_m}) OR ({target_m} AND {context_n})) "
            "RETURN n, type(r) AS relationship, m LIMIT 50"
        )

    def _mechanism_target_neighbors(
        self, text: str, terminology: dict[str, Any]
    ) -> str:
        matched = self._matched_aliases(text, terminology)
        target_terms = matched.get("target") or ["target"]
        target = self._mechanism_match_expression("target", target_terms)
        return (
            "MATCH (target)-[r]-(neighbor) "
            f"WHERE {target} "
            "RETURN target, type(r) AS relationship, neighbor LIMIT 50"
        )

    def _mechanism_context_paths(
        self, text: str, terminology: dict[str, Any]
    ) -> str:
        matched = self._matched_aliases(text, terminology)
        target_terms = matched.get("target") or ["target"]
        context_terms = [
            value
            for entity_type, values in matched.items()
            if entity_type != "target"
            for value in values
        ] or ["disease", "pathway"]
        target = self._mechanism_match_expression("target", target_terms)
        context = self._mechanism_match_expression("context", context_terms)
        return (
            "MATCH path=(target)-[*1..2]-(context) "
            f"WHERE {target} AND {context} "
            "RETURN target, [rel IN relationships(path) | type(rel)] AS relationships, "
            "context LIMIT 50"
        )

    def _mechanism_literature_query(
        self,
        text: str,
        terminology: dict[str, Any],
        *,
        capability: dict[str, Any],
    ) -> str:
        """Build a compact Boolean query from plugin terminology groups."""
        matched = self._matched_aliases(text, terminology)
        groups: list[str] = []
        for values in matched.values():
            ascii_values = list(
                dict.fromkeys(
                    value
                    for value in values
                    if value.isascii() and len(value.strip()) > 1
                )
            )[:5]
            if not ascii_values:
                continue
            rendered = " OR ".join(
                f'"{value}"' if " " in value else value for value in ascii_values
            )
            groups.append(f"({rendered})")
        query = " AND ".join(groups) or str(text or "")
        incompatible = {
            str(value)
            for value in capability.get("incompatible_concepts") or []
        }
        exclusions: list[str] = []
        for canonical_map in terminology.values():
            if not isinstance(canonical_map, dict):
                continue
            for canonical, aliases in canonical_map.items():
                if str(canonical) not in incompatible:
                    continue
                exclusions.extend(
                    str(value)
                    for value in aliases or []
                    if str(value).isascii() and len(str(value).strip()) > 1
                )
        exclusions = list(dict.fromkeys(exclusions))[:8]
        if exclusions:
            rendered = " OR ".join(
                f'"{value}"' if " " in value else value for value in exclusions
            )
            query += f" NOT ({rendered})"
        return query

    @staticmethod
    def _mechanism_match_expression(variable: str, values: list[str]) -> str:
        escaped = [
            value.lower().replace("\\", "\\\\").replace("'", "\\'")
            for value in values
        ]
        terms = ", ".join(f"'{value}'" for value in dict.fromkeys(escaped))
        props = (
            "['name','label','title','node_text','description','id',"
            "'symbol','gene_symbol','synonyms']"
        )
        return (
            f"any(term IN [{terms}] WHERE any(prop IN {props} WHERE "
            f"toLower(toString(coalesce({variable}[prop],''))) CONTAINS term))"
        )
