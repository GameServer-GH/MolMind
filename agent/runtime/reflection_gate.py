"""Reflection Gate: structural + fast-LLM release checks before user-visible finals.

See docs/agent-reflection-gate.md. Gate 0 is code-only; Gate 2 uses
purpose=agent_reflection (deepseek-v4-flash by default).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import json
import os
import re
import time
from typing import Any


_ALLOWED_DECISIONS = frozenset({"release", "rewrite", "reenter", "clarify", "deny"})
_LLM_RELEASE_POINTS = frozenset(
    {"chat_final", "install_followup", "capability_claim", "scp_final"}
)

_FAKE_TOOL_CALL_RE = re.compile(
    r"<tool_call\b|</tool_call>|```(?:json)?\s*\{\s*\"tool\"\s*:",
    re.I,
)
_EXECUTION_CLAIM_RE = re.compile(
    r"(?:正在|已经|已)(?:为你|帮你|给你)?(?:检索|查询|筛选|搜索|导出|生成|调用|执行)|"
    r"(?:已确认安装).{0,40}(?:现在|正在).{0,20}(?:检索|查询|筛选)|"
    r"(?:工具|skill).{0,12}(?:已|已经)(?:启动|调用|完成)",
    re.I,
)
_MISSING_SDF_CLAIM_RE = re.compile(
    r"(?:尚未|没有|未)(?:绑定|检测到|提供).{0,24}(?:SDF|sdf|分子库|化合物库)|"
    r"(?:缺少|需要先上传).{0,16}(?:SDF|sdf|分子库)",
    re.I,
)
_EXTRA_COLUMN_CLAIM_RE = re.compile(
    r"(?:指定|自定义|附加).{0,12}(?:列|字段)|"
    r"(?:CSV|csv).{0,24}(?:附加列|自定义列|任意列)",
    re.I,
)
_INSTALLED_AND_RUNNING_RE = re.compile(
    r"(?:已确认安装|安装成功|已安装).{0,48}(?:现在|正在|开始).{0,24}"
    r"(?:检索|查询|筛选|调用|执行)",
    re.I,
)
_RANKING_MUTATION_CLAIM_RE = re.compile(
    r"(?:已|已经)?(?:重新|再次)?(?:计算|改写|更新|调整|重排).{0,16}(?:排名|主榜|冻结结果|Top\s*\d+)|"
    r"(?:排名|主榜|冻结结果).{0,16}(?:已|已经)(?:更新|改写|重算|调整)",
    re.I,
)
# Soft risks: do not fail Gate 0 alone; they raise Gate 2 priority.
_SOFT_CAPABILITY_INVENTORY_RE = re.compile(
    r"(?:已启用|可安装|插件|技能|能力清单|SCP|MCP|工具列表)",
    re.I,
)
_SOFT_RANKING_LANGUAGE_RE = re.compile(
    r"(?:Top\s*\d+|排名|主榜|冻结结果|提名\s*CSV|候选\s*CSV)",
    re.I,
)
_SOFT_EXECUTION_VERB_RE = re.compile(
    r"(?:检索|查询|筛选|导出|调用|安装|执行|生成PDF|机制PDF)",
    re.I,
)
_SOFT_HALLUCINATED_CSV_FIELDS_RE = re.compile(
    r"\b(?:Rank|SMILES|MASLD_Score|Toxicity_Risk|Molecule_ID)\b"
)

# Issue codes that structural Gate 0 already owns — Gate 2 should not second-guess
# them when we somehow re-enter (defensive; evaluate returns early on structural).
_STRUCTURAL_OWNED_CODES = frozenset(
    {
        "fake_tool_invocation",
        "unsupported_execution_claim",
        "session_library_contradiction",
        "schema_hallucination",
        "install_state_contradiction",
        "overclaim",
    }
)


@dataclass
class ReflectionIssue:
    code: str
    severity: float = 0.8
    evidence: str = ""


@dataclass
class Gate1Decision:
    run_llm: bool
    reason: str
    soft_signals: list[str] = field(default_factory=list)
    sample_rate: float = 1.0


@dataclass
class ReflectionVerdict:
    decision: str = "release"
    confidence: float = 1.0
    source: str = "structural"
    issues: list[ReflectionIssue] = field(default_factory=list)
    diagnosis: str = ""
    repair_hint: dict[str, Any] = field(default_factory=dict)
    release_point: str = ""
    latency_ms: int = 0
    gate1_reason: str = ""
    soft_signals: list[str] = field(default_factory=list)

    def to_memory_dict(self) -> dict[str, Any]:
        return {
            "kind": "reflection_gate",
            "decision": self.decision,
            "confidence": self.confidence,
            "source": self.source,
            "issue_codes": [item.code for item in self.issues],
            "diagnosis": self.diagnosis[:400],
            "release_point": self.release_point,
            "latency_ms": self.latency_ms,
            "gate1_reason": self.gate1_reason[:120],
            "soft_signals": list(self.soft_signals)[:8],
            "repair_hint": dict(self.repair_hint or {}),
            "recorded_at_unix": int(time.time()),
        }


def reflection_gate_mode() -> str:
    raw = os.environ.get("MOLMIND_REFLECTION_GATE", "shadow").strip().lower()
    if raw in {"1", "true", "yes", "on", "enforce"}:
        return "enforce"
    if raw in {"shadow", "dry", "dry-run"}:
        return "shadow"
    if raw in {"0", "false", "no", "off", ""}:
        # Empty still defaults to shadow so a fresh deploy observes; set off explicitly.
        if raw == "":
            return "shadow"
        return "off"
    return "shadow"


def max_reenter() -> int:
    try:
        return max(0, int(os.environ.get("MOLMIND_REFLECTION_MAX_REENTER", "2")))
    except ValueError:
        return 2


def max_rewrite() -> int:
    try:
        return max(0, int(os.environ.get("MOLMIND_REFLECTION_MAX_REWRITE", "1")))
    except ValueError:
        return 1


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return default


def chat_llm_sample_rate() -> float:
    """Fraction of long/low-signal chat_final drafts that still hit Gate 2."""
    return _env_float("MOLMIND_REFLECTION_CHAT_SAMPLE", 0.35)


def scp_llm_sample_rate() -> float:
    """Fraction of short/low-signal scp_final drafts that still hit Gate 2."""
    return _env_float("MOLMIND_REFLECTION_SCP_SAMPLE", 1.0)


def load_reflection_llm_cfg() -> dict[str, Any]:
    """Merge rank_weights ``llm:`` with reflection defaults for Gate 2 / rewrite."""
    return dict(_load_reflection_llm_cfg_cached(_reflection_cfg_fingerprint()))


def _reflection_cfg_fingerprint() -> tuple[str, int | None, int | None]:
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "configs" / "rank_weights.yaml"
    try:
        resolved = path.resolve()
        stat = resolved.stat()
        return (str(resolved), int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return (str(path), None, None)


@lru_cache(maxsize=4)
def _load_reflection_llm_cfg_cached(
    fingerprint: tuple[str, int | None, int | None],
) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "enabled": True,
        "agent_reflection": True,
        "reflection_model": "deepseek-v4-flash",
        "reflection_timeout_sec": 6,
        "reflection_max_tokens": 512,
        "reflection_use_cache": False,
        "reflection_temperature": 0,
    }
    try:
        from pathlib import Path

        import yaml

        path = Path(fingerprint[0])
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            llm = data.get("llm") if isinstance(data, dict) else None
            if isinstance(llm, dict):
                cfg.update(llm)
                cfg["enabled"] = True
                cfg["agent_reflection"] = True
    except Exception:
        pass
    return cfg


def _deterministic_sample(key: str, rate: float) -> bool:
    """Stable per-key sampling (no wall-clock randomness; test-friendly)."""
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) / float(0xFFFFFFFF)
    return bucket < rate


def scan_soft_risk_signals(packet: dict[str, Any]) -> list[str]:
    """Soft risks that raise Gate 2 priority without failing Gate 0 alone."""
    body = str(packet.get("candidate_assistant") or "")
    user = str(packet.get("user_text") or "")
    point = str(packet.get("release_point") or "")
    obs = packet.get("turn_observations") or {}
    signals: list[str] = []
    if _SOFT_CAPABILITY_INVENTORY_RE.search(body) or point == "capability_claim":
        signals.append("capability_inventory")
    if _SOFT_RANKING_LANGUAGE_RE.search(body) or _SOFT_RANKING_LANGUAGE_RE.search(user):
        signals.append("ranking_language")
    if _SOFT_EXECUTION_VERB_RE.search(body) and not (
        obs.get("tool_starts") or obs.get("tool_ends")
    ):
        signals.append("execution_verb_without_tools")
    if _SOFT_HALLUCINATED_CSV_FIELDS_RE.search(body):
        signals.append("csv_field_shape")
    if point == "install_followup" or obs.get("pending_install"):
        signals.append("install_followup")
    if point == "scp_final" and (obs.get("tool_starts") or obs.get("tool_ends")):
        signals.append("scp_tool_summary")
    if len(body) >= 280:
        signals.append("long_draft")
    return list(dict.fromkeys(signals))


def decide_llm_gate(release_point: str, packet: dict[str, Any]) -> Gate1Decision:
    """Gate 1 policy: always / sample / skip based on release point + soft risks."""
    if reflection_gate_mode() == "off":
        return Gate1Decision(False, "gate_off")
    env_flag = os.environ.get("MOLMIND_LLM_REFLECTION", "").strip().lower()
    if env_flag in {"0", "false", "no", "off"}:
        return Gate1Decision(False, "llm_reflection_disabled")
    if env_flag in {"1", "true", "yes", "on", "always"}:
        soft = scan_soft_risk_signals(packet)
        return Gate1Decision(True, "env_force_on", soft, 1.0)

    point = str(release_point or "")
    if point not in _LLM_RELEASE_POINTS:
        return Gate1Decision(False, f"release_point_excluded:{point or 'empty'}")

    soft = scan_soft_risk_signals(packet)
    body = str(packet.get("candidate_assistant") or "")
    obs = packet.get("turn_observations") or {}
    sample_key = (
        f"{point}|{packet.get('user_text', '')[:80]}|{len(body)}|{','.join(soft)}"
    )

    if point in {"install_followup", "capability_claim"}:
        return Gate1Decision(True, f"always:{point}", soft, 1.0)
    if "install_followup" in soft or obs.get("pending_install"):
        return Gate1Decision(True, "always:pending_install", soft, 1.0)
    if any(
        code in soft
        for code in (
            "csv_field_shape",
            "execution_verb_without_tools",
            "capability_inventory",
        )
    ):
        return Gate1Decision(True, f"always:soft:{','.join(soft)}", soft, 1.0)

    if point == "scp_final":
        if "scp_tool_summary" in soft or len(body) >= 160:
            return Gate1Decision(True, "always:scp_summary", soft, 1.0)
        rate = scp_llm_sample_rate()
        hit = _deterministic_sample(sample_key, rate)
        return Gate1Decision(
            hit,
            f"sample:scp_final:{'hit' if hit else 'miss'}:{rate}",
            soft,
            rate,
        )

    if point == "chat_final":
        if "ranking_language" in soft or "long_draft" in soft:
            if reflection_gate_mode() == "enforce" and "ranking_language" in soft:
                return Gate1Decision(True, "always:enforce_ranking_language", soft, 1.0)
            rate = chat_llm_sample_rate()
            hit = _deterministic_sample(sample_key, rate)
            return Gate1Decision(
                hit,
                f"sample:chat_final:{'hit' if hit else 'miss'}:{rate}",
                soft,
                rate,
            )
        return Gate1Decision(False, "skip:harmless_chat", soft, 0.0)

    return Gate1Decision(False, "skip:default", soft, 0.0)


def should_run_llm_gate(release_point: str, packet: dict[str, Any]) -> bool:
    """Backward-compatible Gate 1 boolean wrapper."""
    return decide_llm_gate(release_point, packet).run_llm


def skip_llm_for_structural_codes(issue_codes: list[str] | set[str]) -> bool:
    """True when Gate 0 already owns the issues — do not burn Gate 2 budget."""
    codes = {str(c) for c in issue_codes}
    return bool(codes & _STRUCTURAL_OWNED_CODES)


def build_reflection_packet(
    *,
    session: Any,
    user_text: str,
    candidate_assistant: str,
    route: str,
    release_point: str,
    capability_surface: dict[str, Any] | None = None,
    tool_starts: list[str] | None = None,
    tool_ends: list[str] | None = None,
    install_request_emitted: bool = False,
    registry: Any | None = None,
    scp_catalog: Any | None = None,
) -> dict[str, Any]:
    """Assemble the factual packet for Gate 0 / Gate 2."""
    from agent.runtime.capability_context import build_capability_surface

    surface = capability_surface
    if surface is None and registry is not None:
        surface = build_capability_surface(
            registry, session, scp_catalog=scp_catalog
        )
    surface = surface or {}
    session_library = surface.get("session_library") or {}
    nomination_csv = surface.get("nomination_csv") or {}
    has_sdf = bool(getattr(session, "sdf_bytes", None)) or bool(
        session_library.get("has_sdf")
    )
    pending_install = getattr(session, "pending_install", None)
    if not isinstance(pending_install, dict):
        pending_install = None

    enabled_skill_ids = [
        str(item.get("skill_id") or "")
        for item in surface.get("available_skills") or []
        if item.get("skill_id")
    ]
    for item in surface.get("installed_scp_skills") or []:
        sid = str(item.get("skill_id") or "")
        if sid and item.get("enabled", True) and sid not in enabled_skill_ids:
            enabled_skill_ids.append(sid)

    starts = list(tool_starts or [])
    ends = list(tool_ends or [])
    hard_facts: list[str] = []
    if not starts and not ends:
        hard_facts.append("no_tool_events_this_turn")
    else:
        hard_facts.append(f"tool_starts={starts}")
        hard_facts.append(f"tool_ends={ends}")
    hard_facts.append(f"has_sdf={has_sdf}")
    hard_facts.append(
        f"nomination_csv.schema_locked={bool(nomination_csv.get('schema_locked'))}"
    )
    hard_facts.append(
        "nomination_csv.user_selectable_columns="
        f"{bool(nomination_csv.get('user_selectable_columns'))}"
    )
    # SCP / MCP live evidence must never rewrite the frozen main ranking.
    if str(release_point or "") == "scp_final" or str(route or "") in {
        "scp",
        "execute",
    }:
        hard_facts.append("scp_participates_in_ranking=false")
        hard_facts.append("frozen_ranking_immutable=true")
    if pending_install:
        hard_facts.append(
            "pending_install.skills="
            + ",".join(str(x) for x in (pending_install.get("skill_ids") or []))
        )

    recent: list[dict[str, str]] = []
    for message in (getattr(session, "messages", None) or [])[-8:]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        body = str(message.get("text") or "").strip()
        if role in {"user", "assistant"} and body:
            recent.append({"role": role, "text": body[:500]})

    return {
        "user_text": str(user_text or "")[:1000],
        "candidate_assistant": str(candidate_assistant or "")[:4000],
        "route": str(route or ""),
        "release_point": str(release_point or ""),
        "turn_observations": {
            "tool_starts": starts,
            "tool_ends": ends,
            "install_request_emitted": bool(install_request_emitted),
            "pending_install": pending_install,
            "has_sdf": has_sdf,
            "sdf_filename": str(getattr(session, "sdf_filename", "") or "")
            if has_sdf
            else "",
        },
        "capability_surface_digest": {
            "session_library": {
                "has_sdf": has_sdf,
                "sdf_filename": session_library.get("sdf_filename") or "",
            },
            "nomination_csv": {
                "schema_locked": bool(nomination_csv.get("schema_locked")),
                "user_selectable_columns": bool(
                    nomination_csv.get("user_selectable_columns")
                ),
                "columns_preview": list(nomination_csv.get("columns_preview") or [])[
                    :8
                ],
            },
            "enabled_skill_ids": enabled_skill_ids[:24],
            "policy_notes_head": list(surface.get("policy_notes") or [])[:4],
            "discussable_execution_options": [
                item.get("id")
                for item in surface.get("discussable_execution_options") or []
                if isinstance(item, dict) and item.get("id")
            ][:8],
        },
        "recent_messages": recent,
        "hard_facts": hard_facts,
    }


def run_structural_gate(packet: dict[str, Any]) -> ReflectionVerdict | None:
    """Gate 0: return a reject verdict or None when structurally clean."""
    body = str(packet.get("candidate_assistant") or "")
    obs = packet.get("turn_observations") or {}
    digest = packet.get("capability_surface_digest") or {}
    session_library = digest.get("session_library") or {}
    nomination_csv = digest.get("nomination_csv") or {}
    starts = list(obs.get("tool_starts") or [])
    ends = list(obs.get("tool_ends") or [])
    has_tool_events = bool(starts or ends)
    issues: list[ReflectionIssue] = []

    if _FAKE_TOOL_CALL_RE.search(body) and not has_tool_events:
        issues.append(
            ReflectionIssue(
                "fake_tool_invocation",
                1.0,
                "candidate contains <tool_call> without tool events",
            )
        )
    if _EXECUTION_CLAIM_RE.search(body) and not has_tool_events:
        issues.append(
            ReflectionIssue(
                "unsupported_execution_claim",
                0.95,
                "execution claim without tool_start/tool_end",
            )
        )
    if bool(session_library.get("has_sdf") or obs.get("has_sdf")) and _MISSING_SDF_CLAIM_RE.search(
        body
    ):
        issues.append(
            ReflectionIssue(
                "session_library_contradiction",
                0.95,
                "claims missing SDF while session_library.has_sdf=true",
            )
        )
    if (
        nomination_csv.get("schema_locked")
        and not nomination_csv.get("user_selectable_columns")
        and _EXTRA_COLUMN_CLAIM_RE.search(body)
    ):
        issues.append(
            ReflectionIssue(
                "schema_hallucination",
                0.9,
                "claims user-selectable CSV columns while schema is locked",
            )
        )

    pending = obs.get("pending_install")
    if isinstance(pending, dict) and pending.get("skill_ids"):
        # Still waiting on install but draft claims installed+running.
        if _INSTALLED_AND_RUNNING_RE.search(body) and not has_tool_events:
            issues.append(
                ReflectionIssue(
                    "install_state_contradiction",
                    0.95,
                    "claims installed+running while pending_install is set",
                )
            )

    hard_facts = [str(item) for item in (packet.get("hard_facts") or [])]
    ranking_immutable = any(
        item.startswith("frozen_ranking_immutable=true")
        or item.startswith("scp_participates_in_ranking=false")
        for item in hard_facts
    )
    if ranking_immutable and _RANKING_MUTATION_CLAIM_RE.search(body):
        issues.append(
            ReflectionIssue(
                "overclaim",
                0.95,
                "claims ranking mutation while SCP evidence cannot rewrite frozen ranking",
            )
        )

    if not issues:
        return None

    primary = issues[0].code
    release_point = str(packet.get("release_point") or "")
    decision = "reenter" if primary in {
        "fake_tool_invocation",
        "unsupported_execution_claim",
        "missing_tool",
        "install_state_contradiction",
    } else "clarify"
    if primary in {
        "session_library_contradiction",
        "schema_hallucination",
        "overclaim",
    }:
        decision = "rewrite"
    # After tools already ran, prefer rewrite/clarify over another full reenter.
    if release_point == "scp_final" and decision == "reenter":
        decision = "rewrite"

    retry_text = str(packet.get("user_text") or "").strip()
    pending = obs.get("pending_install")
    if isinstance(pending, dict) and pending.get("retry_text"):
        retry_text = str(pending.get("retry_text") or retry_text).strip()

    preferred_act = "chat"
    if primary in {"fake_tool_invocation", "unsupported_execution_claim", "install_state_contradiction"}:
        preferred_act = "scp"
    if primary == "session_library_contradiction":
        preferred_act = "execute"
    if release_point == "scp_final":
        preferred_act = "chat"

    diagnosis_map = {
        "fake_tool_invocation": "终稿伪造了工具调用语法，但本轮没有真实 tool 事件。",
        "unsupported_execution_claim": "终稿声称已执行/正在执行，但本轮没有工具观察支持。",
        "session_library_contradiction": "会话已绑定 SDF，终稿却声称缺少分子库。",
        "schema_hallucination": "提名 CSV schema 已锁定，终稿却声称可指定附加列。",
        "install_state_contradiction": "安装尚未完成或未续接，终稿却声称已安装并开始执行。",
        "overclaim": "终稿声称改写了冻结主榜/排名，但 SCP 实时资料不得参与排序。",
    }
    rewrite_bits = [
        "纠正与会话真相面矛盾的断言；禁止伪 tool_call；",
        "不得声称已执行未发生的工具；CSV 列以锁定 schema 为准。",
    ]
    if release_point == "scp_final" or ranking_immutable:
        rewrite_bits.append(
            "不得声称重算/改写冻结排名；明确实时资料仅作补充证据。"
        )
    return ReflectionVerdict(
        decision=decision,
        confidence=0.99,
        source="structural",
        issues=issues,
        diagnosis=diagnosis_map.get(primary, "结构闸未通过。"),
        repair_hint={
            "preferred_act": preferred_act,
            "retry_text": retry_text,
            "required_tools": [],
            "rewrite_instruction": (
                "".join(rewrite_bits) if decision == "rewrite" else ""
            ),
        },
        release_point=release_point,
    )


def fuse_repeated_issues(
    session: Any, verdict: ReflectionVerdict
) -> ReflectionVerdict:
    """Force clarify when the same issue codes repeat within a turn (anti-loop)."""
    if verdict.decision == "release" or not verdict.issues:
        return verdict
    codes = tuple(sorted({item.code for item in verdict.issues}))
    active = getattr(session, "active_run", None)
    if not isinstance(active, dict):
        return verdict
    prior = active.get("reflection_last_issue_codes")
    active["reflection_last_issue_codes"] = list(codes)
    session.active_run = active
    if not prior or tuple(prior) != codes:
        return verdict
    if verdict.decision in {"clarify", "deny"}:
        return verdict
    return ReflectionVerdict(
        decision="clarify",
        confidence=max(verdict.confidence, 0.8),
        source=verdict.source,
        issues=list(verdict.issues),
        diagnosis=(
            f"{verdict.diagnosis}（同一问题连续两次未修复，已降级为澄清。）"
        ).strip(),
        repair_hint=dict(verdict.repair_hint),
        release_point=verdict.release_point,
        latency_ms=verdict.latency_ms,
        gate1_reason=verdict.gate1_reason,
        soft_signals=list(verdict.soft_signals),
    )


def rewrite_candidate_text(
    *,
    candidate: str,
    instruction: str,
    user_text: str = "",
    hard_facts: list[str] | None = None,
) -> str:
    """One-shot constrained rewrite for Gate rewrite decisions (chat/scp)."""
    try:
        from plugins.molmind_core.scientific.mechanism.llm_client import (
            chat_completion,
            resolve_llm_settings,
        )
    except Exception:
        return ""

    settings = resolve_llm_settings(
        load_reflection_llm_cfg(), purpose="agent_reflection"
    )
    if not settings.ready:
        return ""
    facts = "\n".join(f"- {item}" for item in (hard_facts or [])[:12]) or "- (none)"
    system = (
        "你是 MolMind 终稿改写器。只输出可对用户展示的中文终稿，不要 JSON、不要伪 tool_call。"
        "必须遵守 hard_facts；不得声称改写冻结主榜；不得编造未发生的工具执行。"
    )
    user = (
        f"用户原话：{user_text[:500]}\n"
        f"改写要求：{instruction}\n"
        f"hard_facts:\n{facts}\n\n"
        f"待改写终稿：\n{candidate[:6000]}\n\n"
        "请给出纠正后的最终回复。"
    )
    try:
        reply = chat_completion(settings, system=system, user=user).strip()
    except Exception:
        return ""
    if _FAKE_TOOL_CALL_RE.search(reply or ""):
        return ""
    return reply


def normalize_verdict(
    data: dict[str, Any] | None,
    *,
    source: str,
    release_point: str = "",
    mode: str = "shadow",
) -> ReflectionVerdict:
    """Coerce model/structural output into a safe ReflectionVerdict."""
    raw = data if isinstance(data, dict) else {}
    decision = str(raw.get("decision") or "release").strip().lower()
    if decision not in _ALLOWED_DECISIONS:
        if mode == "enforce":
            return ReflectionVerdict(
                decision="clarify",
                confidence=0.4,
                source="llm_fallback",
                issues=[ReflectionIssue("other", 0.4, "invalid_decision")],
                diagnosis="反思闸返回非法 decision，已降级为澄清。",
                release_point=release_point,
            )
        return ReflectionVerdict(
            decision="release",
            confidence=0.3,
            source="llm_fallback",
            issues=[ReflectionIssue("other", 0.3, "invalid_decision")],
            diagnosis="反思闸返回非法 decision（shadow 放行）。",
            release_point=release_point,
        )

    issues: list[ReflectionIssue] = []
    for item in raw.get("issues") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "other").strip() or "other"
        issues.append(
            ReflectionIssue(
                code=code,
                severity=float(item.get("severity") or 0.5),
                evidence=str(item.get("evidence") or "")[:240],
            )
        )
    hint = raw.get("repair_hint") if isinstance(raw.get("repair_hint"), dict) else {}
    return ReflectionVerdict(
        decision=decision,
        confidence=float(raw.get("confidence") or 0.5),
        source=source,
        issues=issues,
        diagnosis=str(raw.get("diagnosis") or "")[:400],
        repair_hint={
            "preferred_act": str(hint.get("preferred_act") or "chat"),
            "retry_text": str(hint.get("retry_text") or ""),
            "required_tools": [
                str(x) for x in (hint.get("required_tools") or []) if str(x)
            ][:8],
            "rewrite_instruction": str(hint.get("rewrite_instruction") or "")[:400],
        },
        release_point=release_point,
    )


def run_llm_gate(packet: dict[str, Any]) -> ReflectionVerdict:
    """Gate 2: fast reflection model (deepseek-v4-flash via agent_reflection)."""
    started = time.time()
    mode = reflection_gate_mode()
    try:
        from plugins.molmind_core.scientific.mechanism.llm_client import (
            chat_completion,
            resolve_llm_settings,
        )
    except Exception as exc:  # noqa: BLE001
        return ReflectionVerdict(
            decision="release" if mode != "enforce" else "clarify",
            confidence=0.2,
            source="llm_fallback",
            issues=[ReflectionIssue("other", 0.2, f"import_error:{exc}")],
            diagnosis="反思 LLM 不可用。",
            release_point=str(packet.get("release_point") or ""),
            latency_ms=int((time.time() - started) * 1000),
        )

    settings = resolve_llm_settings(
        load_reflection_llm_cfg(), purpose="agent_reflection"
    )
    if not settings.ready:
        return ReflectionVerdict(
            decision="release",
            confidence=0.2,
            source="llm_fallback",
            issues=[ReflectionIssue("other", 0.2, "llm_not_ready")],
            diagnosis="反思 LLM 未就绪，跳过 Gate 2。",
            release_point=str(packet.get("release_point") or ""),
            latency_ms=int((time.time() - started) * 1000),
        )

    system = (
        "你是 MolMind Reflection Gate。只返回 JSON，不要 Markdown。"
        "格式：{\"decision\":\"release|rewrite|reenter|clarify|deny\","
        "\"confidence\":0-1,\"issues\":[{\"code\":\"...\",\"severity\":0-1,\"evidence\":\"...\"}],"
        "\"diagnosis\":\"中文一句话\",\"repair_hint\":{\"preferred_act\":\"execute|scp|chat|clarify|propose_install\","
        "\"retry_text\":\"\",\"required_tools\":[],\"rewrite_instruction\":\"\"}}。"
        "只能依据 packet 中的 hard_facts / capability_surface_digest / turn_observations。"
        "不得发明未列出的 skill；不得要求改写冻结主榜。"
        "若终稿含伪 tool_call 或声称已执行但无 tool 事件 → reenter。"
        "若与 has_sdf / CSV schema_locked 矛盾 → rewrite 或 clarify。"
        "若声称改写/重算冻结排名但 hard_facts 含 frozen_ranking_immutable=true → rewrite。"
        "scp_final 放行点：工具已发生时不要 reenter 空转，优先 rewrite 纠正越权断言。"
        "普通无害闲聊 → release。"
        "issue.code 限："
        "fake_tool_invocation|unsupported_execution_claim|context_ignored|"
        "missing_tool|schema_hallucination|overclaim|other。"
    )
    user = (
        "请审查候选终稿是否可对用户放行。\n"
        + json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    )
    try:
        raw = chat_completion(settings, system=system, user=user).strip()
        match = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(match.group(0) if match else raw)
    except Exception as exc:  # noqa: BLE001
        return ReflectionVerdict(
            decision="release" if mode != "enforce" else "clarify",
            confidence=0.2,
            source="llm_fallback",
            issues=[ReflectionIssue("other", 0.2, f"llm_error:{type(exc).__name__}")],
            diagnosis="反思 LLM 调用失败。",
            release_point=str(packet.get("release_point") or ""),
            latency_ms=int((time.time() - started) * 1000),
        )

    verdict = normalize_verdict(
        data if isinstance(data, dict) else None,
        source="llm",
        release_point=str(packet.get("release_point") or ""),
        mode=mode,
    )
    verdict.latency_ms = int((time.time() - started) * 1000)
    return verdict


def evaluate_candidate(
    *,
    session: Any,
    user_text: str,
    candidate_assistant: str,
    route: str,
    release_point: str,
    capability_surface: dict[str, Any] | None = None,
    tool_starts: list[str] | None = None,
    tool_ends: list[str] | None = None,
    install_request_emitted: bool = False,
    registry: Any | None = None,
    scp_catalog: Any | None = None,
) -> ReflectionVerdict:
    """Run Gate 0 → optional Gate 2 and return the final verdict."""
    mode = reflection_gate_mode()
    if mode == "off":
        return ReflectionVerdict(
            decision="release",
            confidence=1.0,
            source="structural",
            diagnosis="reflection gate off",
            release_point=release_point,
        )

    packet = build_reflection_packet(
        session=session,
        user_text=user_text,
        candidate_assistant=candidate_assistant,
        route=route,
        release_point=release_point,
        capability_surface=capability_surface,
        tool_starts=tool_starts,
        tool_ends=tool_ends,
        install_request_emitted=install_request_emitted,
        registry=registry,
        scp_catalog=scp_catalog,
    )
    structural = run_structural_gate(packet)
    if structural is not None:
        # Gate 0 owns these issue codes; skip Gate 2 entirely.
        structural.gate1_reason = "skip:structural_owned"
        structural.soft_signals = scan_soft_risk_signals(packet)
        return fuse_repeated_issues(session, structural)

    gate1 = decide_llm_gate(release_point, packet)
    if gate1.run_llm:
        verdict = run_llm_gate(packet)
        verdict.gate1_reason = gate1.reason
        verdict.soft_signals = list(gate1.soft_signals)
        return fuse_repeated_issues(session, verdict)

    return ReflectionVerdict(
        decision="release",
        confidence=1.0,
        source="structural",
        diagnosis=f"structural pass; llm gate skipped ({gate1.reason})",
        release_point=release_point,
        gate1_reason=gate1.reason,
        soft_signals=list(gate1.soft_signals),
    )


def apply_mode(verdict: ReflectionVerdict, *, mode: str | None = None) -> ReflectionVerdict:
    """In shadow mode, keep the diagnosis but force release for the caller."""
    resolved = mode or reflection_gate_mode()
    if resolved != "shadow" or verdict.decision == "release":
        return verdict
    shadowed = ReflectionVerdict(
        decision="release",
        confidence=verdict.confidence,
        source=verdict.source,
        issues=list(verdict.issues),
        diagnosis=f"[shadow-would-{verdict.decision}] {verdict.diagnosis}",
        repair_hint=dict(verdict.repair_hint),
        release_point=verdict.release_point,
        latency_ms=verdict.latency_ms,
        gate1_reason=verdict.gate1_reason,
        soft_signals=list(verdict.soft_signals),
    )
    return shadowed


def clarification_for_verdict(verdict: ReflectionVerdict, *, user_text: str = "") -> str:
    """User-visible clarify/deny text when enforce blocks a draft."""
    codes = {item.code for item in verdict.issues}
    if "fake_tool_invocation" in codes or "unsupported_execution_claim" in codes:
        return (
            "上一稿包含未实际执行的工具调用或执行声明，已被系统拦截，不会对你假装检索/筛选。"
            "若需要文献检索或筛选，请确认相关能力已安装后回复「继续」，或直接重述目标；"
            "我会走真实工具路径。"
        )
    if "session_library_contradiction" in codes:
        return (
            "会话化合物库已绑定。刚才的回复错误声称缺少 SDF，已被拦截。"
            "你可以直接要求生成 TopN 候选 CSV，或说明还想确认哪些执行选项。"
        )
    if "schema_hallucination" in codes:
        return (
            "提名 CSV 的列集合是锁定契约，不能在执行时指定附加列。"
            "可确认的选项包括 TopN、是否导出 CSV/机制 PDF/结果包，以及是否另跑旁证 enrich。"
            "需要的话我可以按真实列示例说明字段含义。"
        )
    if "install_state_contradiction" in codes:
        return (
            "相关能力尚未确认安装完成，或安装后的原请求尚未续接。"
            "请先在安装请求卡片中确认；安装成功后回复「继续」即可按原请求执行。"
        )
    if "overclaim" in codes:
        return (
            "实时文献与知识图谱只能作为补充证据，不能改写已经冻结的候选排名。"
            "刚才的总结触及了这一边界，已被拦截。如需新排名，请明确发起新的筛选请求。"
        )
    if verdict.decision == "deny":
        return (
            "该请求触及科研治理边界，本轮不会执行。"
            f"{verdict.diagnosis}".strip()
        )
    diagnosis = str(verdict.diagnosis or "").strip()
    if diagnosis:
        return (
            f"{diagnosis}"
            "请换一种说法确认目标，或回复「继续」以续接未完成请求。"
        )
    return (
        "本轮回复未通过内部复核，未对你展示不可靠终稿。"
        "请再试一次，或更具体地说明要检索/筛选/导出的目标。"
        + (f"（原话：{user_text[:80]}）" if user_text else "")
    )


def packet_hash(packet: dict[str, Any]) -> str:
    blob = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def record_gate_on_session(
    session: Any,
    verdict: ReflectionVerdict,
    *,
    packet: dict[str, Any] | None = None,
) -> None:
    """Append a compact reflection_gate record to working_memory."""
    memory = list(getattr(session, "working_memory", None) or [])
    item = verdict.to_memory_dict()
    if packet is not None:
        item["packet_hash"] = packet_hash(packet)
    active = getattr(session, "active_run", None) or {}
    item["reflection_depth"] = int(active.get("reflection_depth") or 0)
    memory.append(item)
    session.working_memory = memory[-24:]


def bump_reflection_counter(session: Any, key: str = "reflection_depth") -> int:
    active = getattr(session, "active_run", None)
    if not isinstance(active, dict):
        return 0
    value = int(active.get(key) or 0) + 1
    active[key] = value
    session.active_run = active
    return value
