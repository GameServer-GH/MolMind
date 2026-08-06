"""Execution-gate dialog acts: principle-based LLM, not pause-phrase lists."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.intent import parse_intent
from agent.registry import get_registry
from agent.runtime.loop import (
    AgentRuntime,
    _is_direct_deliverable_request,
    _offline_prefer_discuss,
)
from agent.runtime.task_router import TaskRouter


def _runtime_stub() -> AgentRuntime:
    """Build a runtime without touching Postgres-backed FileRunStore."""
    rt = AgentRuntime.__new__(AgentRuntime)
    rt.store = SimpleNamespace(
        persist=lambda session: None,
        create=lambda **kwargs: SimpleNamespace(),
    )
    rt.registry = get_registry()
    rt.task_router = TaskRouter(rt.registry)
    return rt


def _session(**kwargs):
    base = {
        "messages": [],
        "last_result": None,
        "pending_goal": None,
        "pending_action": None,
        "run_history": [],
        "top_n": 10,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_direct_deliverable_no_longer_bypasses_on_bare_screening_discuss() -> None:
    text = "先别跑筛选，只讨论筛选条件怎么设"
    intent = parse_intent(text)
    assert intent.wants_tools is True
    assert _is_direct_deliverable_request(intent, text) is False


def test_direct_deliverable_still_matches_explicit_topn_run() -> None:
    text = "帮我跑一轮 MASLD 低毒降脂筛选，输出 Top10"
    intent = parse_intent(text)
    assert _is_direct_deliverable_request(intent, text) is True


def test_force_rescreen_structural_flag() -> None:
    intent = parse_intent("忽略之前条件，按默认配置重新筛选")
    assert intent.force_rescreen is True
    assert intent.want_csv is True


def test_bundle_alias_候选包() -> None:
    intent = parse_intent("给我候选包 / bundle")
    assert intent.want_bundle is True
    assert "masld_export_bundle" in intent.skill_ids


def test_front_rank_span_is_ranking_explain() -> None:
    intent = parse_intent("前 5 名里哪个更适合继续推进？")
    assert intent.explain_ranking is True
    assert intent.ranking_positions == (1, 2, 3, 4, 5)
    assert intent.wants_tools is False


@pytest.mark.parametrize(
    ("text", "dialog_act", "gate"),
    [
        # These are tool-shaped after Intent (contain screening vocabulary) and
        # must be stopped by the LLM gate—not by a pause-phrase table.
        ("先别跑筛选，只讨论筛选条件怎么设", "discuss_only", "block"),
        ("按默认配置重新筛选", "execute_now", "allow"),
        ("帮我跑一轮 MASLD 低毒降脂筛选，输出 Top10", "execute_now", "allow"),
        ("忽略之前条件，按默认配置重新筛选", "execute_now", "allow"),
    ],
)
def test_execution_gate_uses_llm_principles_not_phrase_table(
    text: str, dialog_act: str, gate: str, monkeypatch
) -> None:
    def fake_object(**kwargs):
        system = kwargs["system"]
        # Guardrails: no stop/continue phrase whitelist in the gate prompt.
        assert "停止执行" not in system
        assert "继续执行" not in system
        assert "先别跑" not in system
        assert "口令" in system or "白名单" in system or "规则表" in system
        return {
            "dialog_act": dialog_act,
            "execution_gate": gate,
            "force_rescreen": bool(gate == "allow" and "重新筛选" in text),
            "reason": "test",
        }, "ok"

    monkeypatch.setattr("agent.runtime.loop.llm_json_object", fake_object)
    rt = _runtime_stub()
    intent = parse_intent(text)
    assert intent.wants_tools is True
    got_gate, meta = rt._classify_execution_gate(_session(), text, intent)
    assert got_gate == gate
    assert meta["dialog_act"] == dialog_act


def test_non_tool_defer_phrases_stay_chat_without_gate() -> None:
    """Utterances without deliverable surface never enter the tool path."""
    for text in (
        "跳过执行，我们先把毒性阈值说清楚",
        "这轮先 hold 住 pipeline",
        "先别动工具，我问配置",
        "停，条件还没定",
    ):
        intent = parse_intent(text)
        assert intent.wants_tools is False


def test_gate_block_rewrites_tool_intent_before_router(monkeypatch) -> None:
    """Simulate the early turn rewrite that prevents core execution."""
    monkeypatch.setattr(
        "agent.runtime.loop.llm_json_object",
        lambda **kwargs: (
            {
                "dialog_act": "discuss_only",
                "execution_gate": "block",
                "force_rescreen": False,
                "reason": "user wants to discuss config only",
            },
            "ok",
        ),
    )
    rt = _runtime_stub()
    text = "先别跑筛选，只讨论筛选条件怎么设"
    intent = parse_intent(text)
    assert intent.wants_tools is True
    gate, meta = rt._classify_execution_gate(_session(), text, intent)
    assert gate == "block"
    assert meta["dialog_act"] == "discuss_only"
    # After block, turn code clears tool flags (mirrors loop rewrite).
    from dataclasses import replace

    blocked = replace(
        intent,
        wants_tools=False,
        want_csv=False,
        skill_ids=(),
        execution_requested=False,
        reason="一般对话，暂不调用筛选工具",
    )
    assert rt.task_router.route(blocked, _session()).route == "chat"


def test_clarify_route_does_not_always_claim_missing_freeze() -> None:
    rt = _runtime_stub()
    route = SimpleNamespace(
        reason="用户未提供评估标准，需补充信息",
        label="需要澄清",
    )
    assert "冻结" not in rt._clarify_reply_for_route(route)
    missing = SimpleNamespace(
        reason="ranking_followup_missing_frozen_result",
        label="缺少冻结结果",
    )
    assert "冻结筛选结果" in rt._clarify_reply_for_route(missing)
    install = SimpleNamespace(
        reason="scp_skill_not_installed:literature_research",
        label="文献检索",
    )
    install_text = rt._clarify_reply_for_route(install)
    assert "工具与插件" not in install_text
    assert "安装" in install_text


def test_need_screen_forced_on_rescreen_flag(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.runtime.loop.llm_json_object",
        lambda **kwargs: (
            {
                "dialog_act": "execute_now",
                "execution_gate": "allow",
                "force_rescreen": True,
                "reason": "rescreen",
            },
            "ok",
        ),
    )
    rt = _runtime_stub()
    session = _session(
        last_result=SimpleNamespace(top_molecules=[object()] * 20, run_id="mm-old")
    )
    intent = parse_intent("忽略之前条件，按默认配置重新筛选")
    assert intent.force_rescreen is True
    gate, meta = rt._classify_execution_gate(session, intent.raw_text, intent)
    assert gate == "allow"
    assert meta["force_rescreen"] is True


def test_offline_gate_blocks_discuss_shaped_tool_surface(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.runtime.loop.llm_json_object",
        lambda **kwargs: (kwargs["default"], "llm_not_ready"),
    )
    rt = _runtime_stub()
    text = "先别跑筛选，只讨论筛选条件怎么设"
    intent = parse_intent(text)
    gate, meta = rt._classify_execution_gate(_session(), text, intent)
    assert gate == "block"
    assert meta["dialog_act"] == "discuss_only"


def test_offline_gate_allows_用默认配置筛选_top20(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.runtime.loop.llm_json_object",
        lambda **kwargs: (kwargs["default"], "llm_unavailable:MechanismLLMError"),
    )
    rt = _runtime_stub()
    text = "用默认配置筛选 Top20"
    intent = parse_intent(text)
    assert intent.requested_top_n == 20
    assert _is_direct_deliverable_request(intent, text) is True
    gate, meta = rt._classify_execution_gate(_session(), text, intent)
    assert gate == "allow"
    assert meta["dialog_act"] == "execute_now"


def test_gate_block_skips_scp_even_when_topn_in_text() -> None:
    rt = _runtime_stub()
    session = _session(
        turn_execution_gate="block",
        turn_execution_dialog_act="clarify",
        installed_scp_skills={"literature_research": {"enabled": True}},
    )
    intent = parse_intent("用默认配置筛选 Top20")
    # Mirror post-block rewrite: tools cleared, but raw text still has Top20.
    from dataclasses import replace

    blocked = replace(
        intent,
        wants_tools=False,
        want_csv=False,
        skill_ids=(),
        execution_requested=False,
        reason="一般对话，暂不调用筛选工具",
    )
    route = rt.task_router.route(blocked, session)
    assert route.route == "clarify"
    assert route.reason == "execution_gate_block:clarify"
    assert "literature" not in route.reason.lower()


def test_gate_block_discuss_routes_chat_not_deny() -> None:
    rt = _runtime_stub()
    session = _session(
        turn_execution_gate="block",
        turn_execution_dialog_act="discuss_only",
        installed_scp_skills={"literature_research": {"enabled": True}},
    )
    intent = parse_intent("先别跑筛选，只讨论筛选条件怎么设")
    from dataclasses import replace

    blocked = replace(
        intent,
        wants_tools=False,
        want_csv=False,
        skill_ids=(),
        execution_requested=False,
    )
    route = rt.task_router.route(blocked, session)
    assert route.route == "chat"
    assert route.reason.startswith("execution_gate_block:")


def test_deny_reply_does_not_reuse_freeze_boundary_for_scp() -> None:
    rt = _runtime_stub()
    scp_deny = SimpleNamespace(
        reason="能力 literature_search 仅支持默认 top_k=5，不支持用户指定 top20",
        label="参数不支持",
    )
    text = rt._deny_reply_for_route(scp_deny)
    assert "冻结候选的排序只能由 MolMind Core" not in text
    assert "literature_search" in text or "top20" in text or "参数" in text

    freeze_deny = SimpleNamespace(
        reason="frozen_ranking_boundary_scp_cannot_rewrite_selection",
        label="拒绝改写冻结排名",
    )
    assert "MolMind Core" in rt._deny_reply_for_route(freeze_deny)


def test_attach_sdf_same_bytes_preserves_freeze() -> None:
    rt = _runtime_stub()
    saved = {}

    def save_sdf(session):
        saved["ok"] = True

    rt.store.save_sdf = save_sdf
    session = SimpleNamespace(
        sdf_bytes=b"same-sdf",
        sdf_filename="lib.sdf",
        sdf_ui_pending=False,
        last_result=object(),
        frozen_ranking={"run_id": "mm-1", "top_molecules": [{"molecule_id": "T1"}]},
        last_run_id="mm-1",
        last_selection_sha256="abc",
        last_molecule_index={"T1": 0},
        last_mechanism_job_id="job-1",
        active_plan={"goal": "x"},
    )
    rt.attach_sdf(session, filename="lib.sdf", content=b"same-sdf")
    assert session.frozen_ranking is not None
    assert session.last_result is not None
    assert session.last_run_id == "mm-1"

    rt.attach_sdf(session, filename="lib2.sdf", content=b"different-sdf")
    assert session.frozen_ranking is None
    assert session.last_result is None


def test_offline_prefer_discuss_on_compound_message() -> None:
    text = "你好\n解释一下 MASLD 是什么\n先别跑筛选，只讨论筛选条件怎么设"
    assert _offline_prefer_discuss(text) is True
    intent = parse_intent(text)
    assert intent.wants_tools is True
    assert _is_direct_deliverable_request(intent, text) is False


def test_offline_gate_discuss_bias_when_llm_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.runtime.loop.llm_json_object",
        lambda **kwargs: (kwargs["default"], "llm_unavailable:MechanismLLMError"),
    )
    rt = _runtime_stub()
    text = "你好\n解释一下 MASLD 是什么\n先别跑筛选，只讨论筛选条件怎么设"
    intent = parse_intent(text)
    gate, meta = rt._classify_execution_gate(_session(), text, intent)
    assert gate == "block"
    assert meta["dialog_act"] == "discuss_only"


def test_offline_discuss_does_not_override_later_execute() -> None:
    text = "先别跑筛选\n好的，那帮我跑一轮 MASLD 低毒降脂筛选，输出 Top10"
    assert _offline_prefer_discuss(text) is False


def test_force_rescreen_resets_session_top_n_to_profile_default() -> None:
    rt = _runtime_stub()
    session = _session(top_n=50, pending_top_confirm={"requested_top_n": 100, "top_n": 50})
    intent = parse_intent(
        "忽略之前条件，按默认配置重新筛选",
        default_top_n=session.top_n,
    )
    assert intent.force_rescreen is True
    assert intent.requested_top_n is None
    # Simulate the handle_message reset path.
    default_n = rt._profile_default_top_n(session)
    assert default_n == 10
    session.top_n = default_n
    session.pending_top_confirm = None
    refreshed = parse_intent(
        "忽略之前条件，按默认配置重新筛选",
        default_top_n=session.top_n,
    )
    assert refreshed.top_n == 10
    assert refreshed.force_rescreen is True
