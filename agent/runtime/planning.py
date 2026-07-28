"""Declarative planning primitives for the MolMind agent.

The planner is deliberately separate from the scientific pipeline: it may
choose and order capabilities, but it never computes or alters rankings.
Every proposed step is checked against the registry contract before execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Iterable

from agent.registry.models import ToolSpec


@dataclass(frozen=True)
class PlanStep:
    tool_id: str
    args: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


@dataclass(frozen=True)
class AgentPlan:
    goal: str
    action: str  # execute | explain | chat | clarify
    steps: tuple[PlanStep, ...] = ()
    expected_artifacts: tuple[str, ...] = ()
    rationale: str = ""


def llm_plan_request(
    *,
    text: str,
    recent_messages: list[dict[str, Any]],
    tools: dict[str, ToolSpec],
    skills: dict[str, Any],
    capabilities: set[str],
    default_top_n: int = 10,
) -> tuple[AgentPlan | None, str]:
    """Ask the conversational model for a bounded, registry-backed plan.

    A malformed/model-unavailable response returns ``None``. Callers must use
    their conservative offline path in that case; this helper never invents a
    tool step itself.
    """
    try:
        from plugins.molmind_core.scientific.mechanism.llm_client import (
            chat_completion,
            resolve_llm_settings,
        )

        settings = resolve_llm_settings(
            {"enabled": True, "agent_chat": True}, purpose="agent_chat"
        )
        if not settings.ready:
            return None, "llm_not_ready"
        settings = type(settings)(
            enabled=settings.enabled,
            model=settings.model,
            base_url=settings.base_url,
            api_key=settings.api_key,
            temperature=0.0,
            timeout_sec=min(settings.timeout_sec, 12.0),
            max_tokens=min(max(settings.max_tokens, 320), 800),
            cache_dir=settings.cache_dir,
            use_cache=False,
        )
        history = "\n".join(
            f"{m.get('role', '')}: {str(m.get('text', ''))[:400]}"
            for m in recent_messages[-6:]
            if m.get("role") in {"user", "assistant"} and m.get("text")
        ) or "（无）"
        available_skills = [
            {
                "skill_id": skill_id,
                "description": getattr(skill, "description", ""),
                "requires": getattr(skill, "requires", []),
                "produces": getattr(skill, "produces", []),
                "tools": getattr(skill, "tools", []),
            }
            for skill_id, skill in skills.items()
        ]
        system = (
            "你是 MolMind 的受约束任务规划器。只返回 JSON，不要 Markdown。"
            "格式：{\"action\":\"execute|explain|chat|clarify\",\"goal\":\"...\","
            "\"skill_ids\":[...],\"rationale\":\"...\"}。"
            "只能选择下方提供的 skill_id；execute 仅用于明确要求生成、导出、运行筛选或报告；"
            "只要用户明确说“生成/导出/重新运行”并且所需前置条件已满足，必须选 execute，"
            "即使当前已有不同 TopN 的冻结结果；例如“生成 Top11”意味着新运行，不是聊天。"
            "会话默认 TopN 已由运行时提供；用户说“生成 TopN”但未写数字时，"
            "必须使用该默认值并选 execute，不能为 top_n 再次澄清。"
            "用户只给出分子量、LogP、PAINS、靶点偏好等筛选条件时，若工具目录的 input_schema"
            "没有对应参数，必须选 clarify：说明该条件尚未映射为可执行参数，绝不能声称筛选已经运行、"
            "正在运行、已冻结，或虚构 TopN/耗时。"
            "解释已有冻结结果用 explain；不确定或缺少前置条件用 clarify。"
            "不得把科学评分、排名或实验结果编造成计划参数。"
        )
        user = (
            f"会话能力：{sorted(capabilities)}；会话默认 TopN：{int(default_top_n)}\n"
            f"技能目录：{json.dumps(available_skills, ensure_ascii=False)}\n"
            f"工具目录：{json.dumps(tool_catalog(tools, capabilities=capabilities), ensure_ascii=False)}\n"
            f"最近对话：\n{history}\n用户本轮：{text}"
        )
        raw = chat_completion(settings, system=system, user=user).strip()
        match = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(match.group(0) if match else raw)
        action = str(data.get("action") or "").strip().lower()
        if action not in {"execute", "explain", "chat", "clarify"}:
            return None, "llm_invalid_action"
        selected_skills = [
            str(skill_id)
            for skill_id in data.get("skill_ids") or []
            if str(skill_id) in skills
        ]
        plan, diagnostics = plan_for_skills(
            goal=str(data.get("goal") or text).strip()[:300],
            action=action,
            skill_ids=selected_skills,
            skills=skills,
            tools=tools,
            capabilities=set(capabilities),
        )
        return (
            AgentPlan(
                goal=plan.goal,
                action=plan.action,
                steps=plan.steps,
                expected_artifacts=plan.expected_artifacts,
                rationale=str(data.get("rationale") or "").strip()[:500],
            ),
            "llm" if not diagnostics else "llm;" + ";".join(diagnostics),
        )
    except Exception as exc:  # noqa: BLE001 - optional planner
        return None, f"llm_unavailable:{type(exc).__name__}"


def session_capabilities(session: Any) -> set[str]:
    """Return facts a tool contract may depend on; never infer user intent."""
    facts = {"session"}
    if getattr(session, "sdf_bytes", None):
        facts.add("sdf")
    if getattr(session, "last_result", None) is not None:
        facts.add("frozen_result")
    if getattr(session, "last_mechanism_job_id", ""):
        facts.add("mechanism_job")
    return facts


def validate_plan_steps(
    steps: Iterable[PlanStep],
    *,
    tools: dict[str, ToolSpec],
    capabilities: set[str],
) -> tuple[tuple[PlanStep, ...], list[str]]:
    """Keep only registered, policy-compatible steps and return diagnostics."""
    accepted: list[PlanStep] = []
    diagnostics: list[str] = []
    for step in steps:
        tool = tools.get(step.tool_id)
        if tool is None:
            diagnostics.append(f"unknown_tool:{step.tool_id}")
            continue
        missing = [req for req in tool.requires if req not in capabilities]
        if missing:
            diagnostics.append(f"missing_precondition:{step.tool_id}:{','.join(missing)}")
            continue
        accepted.append(step)
        # A plan may satisfy later steps itself (for example score_and_rank
        # produces frozen_result, enabling export_nomination).
        capabilities.update(tool.produces)
    return tuple(accepted), diagnostics


def tool_catalog(tools: dict[str, ToolSpec], *, capabilities: set[str]) -> list[dict[str, Any]]:
    """Compact, dynamic capability view for an LLM planner or UI."""
    out: list[dict[str, Any]] = []
    for tool in tools.values():
        missing = [req for req in tool.requires if req not in capabilities]
        out.append(
            {
                "tool_id": tool.tool_id,
                "description": tool.description,
                "risk": tool.risk,
                "requires": tool.requires,
                "produces": tool.produces,
                "input_schema": tool.input_schema,
                "available": not missing,
                "missing": missing,
                "confirmation_required": tool.confirmation_required,
            }
        )
    return out


def plan_for_skills(
    *,
    goal: str,
    action: str,
    skill_ids: Iterable[str],
    skills: dict[str, Any],
    tools: dict[str, ToolSpec],
    capabilities: set[str],
    executable_tools: set[str] | None = None,
) -> tuple[AgentPlan, list[str]]:
    """Compile declared skill dependencies into a validated execution plan.

    This is intentionally registry-driven: adding a skill with ``tools`` and
    contracts changes the plan without adding another intent keyword branch.
    """
    requested_steps: list[PlanStep] = []
    artifacts: list[str] = []
    for skill_id in skill_ids:
        skill = skills.get(skill_id)
        if skill is None:
            continue
        requested_steps.extend(
            PlanStep(tool_id=tool_id)
            for tool_id in skill.tools
            if executable_tools is None or tool_id in executable_tools
        )
        artifacts.extend(str(x) for x in getattr(skill, "produces", []) or [])
    steps, diagnostics = validate_plan_steps(
        requested_steps, tools=tools, capabilities=set(capabilities)
    )
    return (
        AgentPlan(
            goal=goal,
            action=action,
            steps=steps,
            expected_artifacts=tuple(dict.fromkeys(artifacts)),
        ),
        diagnostics,
    )
