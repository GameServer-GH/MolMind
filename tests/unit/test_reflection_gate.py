"""Reflection Gate: structural checks, verdict contract, and mode behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.registry import get_registry
from agent.runtime.capability_context import build_capability_surface
from agent.runtime.reflection_gate import (
    apply_mode,
    build_reflection_packet,
    clarification_for_verdict,
    evaluate_candidate,
    normalize_verdict,
    reflection_gate_mode,
    run_structural_gate,
)


def _session(**kwargs):
    base = {
        "messages": [],
        "working_memory": [],
        "active_run": {},
        "sdf_bytes": None,
        "sdf_filename": "",
        "pending_install": None,
        "profile_id": "competition_masld",
        "installed_catalog": [],
        "installed_scp_skills": {},
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def _surface(session):
    return build_capability_surface(get_registry(), session)


def test_reflection_gate_mode_defaults_to_shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOLMIND_REFLECTION_GATE", raising=False)
    assert reflection_gate_mode() == "shadow"
    monkeypatch.setenv("MOLMIND_REFLECTION_GATE", "enforce")
    assert reflection_gate_mode() == "enforce"
    monkeypatch.setenv("MOLMIND_REFLECTION_GATE", "off")
    assert reflection_gate_mode() == "off"


def test_structural_rejects_fake_tool_call() -> None:
    session = _session(
        pending_install={
            "skill_ids": ["literature_research"],
            "retry_text": "检索 PPARγ 与 MASLD 文献",
        }
    )
    packet = build_reflection_packet(
        session=session,
        user_text="继续",
        candidate_assistant=(
            "好的，已确认安装。现在开始检索：\n"
            "<tool_call>{\"tool\":\"query_paper\"}</tool_call>"
        ),
        route="chat",
        release_point="install_followup",
        capability_surface=_surface(session),
        tool_starts=[],
        tool_ends=[],
    )
    verdict = run_structural_gate(packet)
    assert verdict is not None
    assert verdict.decision == "reenter"
    assert "fake_tool_invocation" in {i.code for i in verdict.issues}
    assert verdict.repair_hint.get("retry_text") == "检索 PPARγ 与 MASLD 文献"
    assert verdict.repair_hint.get("preferred_act") == "scp"


def test_structural_rejects_missing_sdf_contradiction() -> None:
    session = _session(sdf_bytes=b"mol", sdf_filename="lib.sdf")
    packet = build_reflection_packet(
        session=session,
        user_text="生成 Top50 CSV",
        candidate_assistant="当前尚未绑定 SDF 分子库，请先上传后再筛选。",
        route="chat",
        release_point="chat_final",
        capability_surface=_surface(session),
    )
    verdict = run_structural_gate(packet)
    assert verdict is not None
    assert verdict.decision == "rewrite"
    assert "session_library_contradiction" in {i.code for i in verdict.issues}


def test_structural_rejects_csv_extra_columns() -> None:
    session = _session()
    packet = build_reflection_packet(
        session=session,
        user_text="提名 CSV 有哪些字段？执行时能指定附加列吗？",
        candidate_assistant=(
            "可以。执行时你可以指定附加列，例如 Note、自定义毒性注释等。"
        ),
        route="chat",
        release_point="capability_claim",
        capability_surface=_surface(session),
    )
    verdict = run_structural_gate(packet)
    assert verdict is not None
    assert verdict.decision == "rewrite"
    assert "schema_hallucination" in {i.code for i in verdict.issues}


def test_structural_passes_harmless_chat() -> None:
    session = _session()
    packet = build_reflection_packet(
        session=session,
        user_text="什么是 MASLD？",
        candidate_assistant=(
            "MASLD 是代谢相关脂肪性肝病的英文缩写，"
            "指与代谢紊乱相关的肝脏脂肪堆积。"
        ),
        route="chat",
        release_point="chat_final",
        capability_surface=_surface(session),
    )
    assert run_structural_gate(packet) is None


def test_structural_allows_execution_claim_with_tool_events() -> None:
    session = _session()
    packet = build_reflection_packet(
        session=session,
        user_text="检索文献",
        candidate_assistant="已完成检索，找到 3 篇相关论文。",
        route="scp",
        release_point="scp_final",
        capability_surface=_surface(session),
        tool_starts=["scp:Scholar-KG:query_paper"],
        tool_ends=["scp:Scholar-KG:query_paper"],
    )
    assert run_structural_gate(packet) is None


def test_shadow_mode_forces_release(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOLMIND_REFLECTION_GATE", "shadow")
    session = _session(sdf_bytes=b"x", sdf_filename="a.sdf")
    verdict = evaluate_candidate(
        session=session,
        user_text="继续筛选",
        candidate_assistant="尚未绑定 SDF，无法筛选。",
        route="chat",
        release_point="chat_final",
        capability_surface=_surface(session),
        registry=get_registry(),
    )
    assert verdict.decision == "rewrite"
    shadowed = apply_mode(verdict, mode="shadow")
    assert shadowed.decision == "release"
    assert "shadow-would-rewrite" in shadowed.diagnosis


def test_enforce_mode_keeps_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOLMIND_REFLECTION_GATE", "enforce")
    session = _session()
    verdict = evaluate_candidate(
        session=session,
        user_text="继续",
        candidate_assistant="<tool_call>query_paper</tool_call>",
        route="chat",
        release_point="install_followup",
        capability_surface=_surface(session),
        registry=get_registry(),
    )
    assert verdict.decision == "reenter"
    assert apply_mode(verdict, mode="enforce").decision == "reenter"


def test_normalize_invalid_decision_shadow_releases() -> None:
    verdict = normalize_verdict(
        {"decision": "banana", "confidence": 0.9},
        source="llm",
        mode="shadow",
    )
    assert verdict.decision == "release"
    assert verdict.source == "llm_fallback"


def test_normalize_invalid_decision_enforce_clarifies() -> None:
    verdict = normalize_verdict(
        {"decision": "banana"},
        source="llm",
        mode="enforce",
    )
    assert verdict.decision == "clarify"


def test_structural_rejects_scp_ranking_mutation() -> None:
    session = _session()
    packet = build_reflection_packet(
        session=session,
        user_text="检索 PPAR 文献",
        candidate_assistant="根据检索结果，我已重新计算并更新了冻结主榜的排名。",
        route="scp",
        release_point="scp_final",
        capability_surface=_surface(session),
        tool_starts=["scp:Scholar-KG:query_paper"],
        tool_ends=["scp:Scholar-KG:query_paper"],
    )
    verdict = run_structural_gate(packet)
    assert verdict is not None
    assert verdict.decision == "rewrite"
    assert "overclaim" in {i.code for i in verdict.issues}
    assert "frozen_ranking_immutable=true" in packet["hard_facts"]


def test_scp_final_converts_fake_tool_to_rewrite() -> None:
    """After tools ran, scp_final should rewrite instead of reenter-looping."""
    session = _session()
    # Without tool events, fake tool → reenter; with scp_final + no tools still rewrite
    # when release_point forces conversion. Simulate no events + fake call on scp_final.
    packet = build_reflection_packet(
        session=session,
        user_text="检索",
        candidate_assistant="<tool_call>query_paper</tool_call>",
        route="scp",
        release_point="scp_final",
        capability_surface=_surface(session),
        tool_starts=[],
        tool_ends=[],
    )
    verdict = run_structural_gate(packet)
    assert verdict is not None
    assert verdict.decision == "rewrite"


def test_fuse_repeated_issues_forces_clarify() -> None:
    from agent.runtime.reflection_gate import (
        ReflectionIssue,
        ReflectionVerdict,
        fuse_repeated_issues,
    )

    session = _session(active_run={})
    first = ReflectionVerdict(
        decision="rewrite",
        issues=[ReflectionIssue("schema_hallucination", 0.9, "x")],
        diagnosis="第一次",
    )
    out1 = fuse_repeated_issues(session, first)
    assert out1.decision == "rewrite"
    second = ReflectionVerdict(
        decision="rewrite",
        issues=[ReflectionIssue("schema_hallucination", 0.9, "x")],
        diagnosis="第二次",
    )
    out2 = fuse_repeated_issues(session, second)
    assert out2.decision == "clarify"
    assert "连续两次" in out2.diagnosis


def test_should_run_llm_gate_for_scp_final() -> None:
    from agent.runtime.reflection_gate import decide_llm_gate, should_run_llm_gate

    packet = {
        "candidate_assistant": "x" * 200,
        "user_text": "检索",
        "release_point": "scp_final",
        "turn_observations": {
            "tool_starts": ["scp:a"],
            "tool_ends": ["scp:a"],
        },
    }
    assert should_run_llm_gate("scp_final", packet) is True
    assert decide_llm_gate("scp_final", packet).reason.startswith("always:")
    assert should_run_llm_gate("pre_tool_promise", packet) is False


def test_gate1_skips_harmless_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOLMIND_LLM_REFLECTION", raising=False)
    monkeypatch.setenv("MOLMIND_REFLECTION_GATE", "shadow")
    from agent.runtime.reflection_gate import decide_llm_gate

    packet = {
        "candidate_assistant": "MASLD 是代谢相关脂肪性肝病。",
        "user_text": "什么是 MASLD？",
        "release_point": "chat_final",
        "turn_observations": {"tool_starts": [], "tool_ends": []},
    }
    decision = decide_llm_gate("chat_final", packet)
    assert decision.run_llm is False
    assert decision.reason == "skip:harmless_chat"


def test_gate1_always_on_capability_soft_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOLMIND_LLM_REFLECTION", raising=False)
    monkeypatch.setenv("MOLMIND_REFLECTION_GATE", "shadow")
    from agent.runtime.reflection_gate import decide_llm_gate

    packet = {
        "candidate_assistant": "当前已启用插件 molmind-core，可安装 SCP 技能 literature_research。",
        "user_text": "有哪些能力？",
        "release_point": "chat_final",
        "turn_observations": {"tool_starts": [], "tool_ends": []},
    }
    decision = decide_llm_gate("chat_final", packet)
    assert decision.run_llm is True
    assert "capability_inventory" in decision.soft_signals


def test_gate1_sample_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOLMIND_LLM_REFLECTION", raising=False)
    monkeypatch.setenv("MOLMIND_REFLECTION_GATE", "shadow")
    monkeypatch.setenv("MOLMIND_REFLECTION_CHAT_SAMPLE", "0.5")
    from agent.runtime.reflection_gate import decide_llm_gate

    packet = {
        "candidate_assistant": "x" * 300 + " 关于 Top50 排名说明。",
        "user_text": "解释排名",
        "release_point": "chat_final",
        "turn_observations": {"tool_starts": [], "tool_ends": []},
    }
    a = decide_llm_gate("chat_final", packet)
    b = decide_llm_gate("chat_final", packet)
    assert a.run_llm == b.run_llm
    assert a.reason == b.reason


def test_skip_llm_for_structural_codes() -> None:
    from agent.runtime.reflection_gate import skip_llm_for_structural_codes

    assert skip_llm_for_structural_codes(["schema_hallucination"]) is True
    assert skip_llm_for_structural_codes(["context_ignored"]) is False


def test_load_reflection_llm_cfg_reads_flash_defaults() -> None:
    from agent.runtime.reflection_gate import load_reflection_llm_cfg

    cfg = load_reflection_llm_cfg()
    assert cfg.get("reflection_model") == "deepseek-v4-flash"
    assert float(cfg.get("reflection_timeout_sec") or 0) <= 10


def test_clarification_mentions_fake_tool() -> None:
    from agent.runtime.reflection_gate import ReflectionIssue, ReflectionVerdict

    text = clarification_for_verdict(
        ReflectionVerdict(
            decision="clarify",
            issues=[ReflectionIssue("fake_tool_invocation", 1.0, "x")],
        )
    )
    assert "拦截" in text
    assert "工具" in text
