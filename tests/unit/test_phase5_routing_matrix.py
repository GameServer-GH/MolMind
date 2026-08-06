"""Phase 5 acceptance matrix: plugin routes never cross the Core write boundary."""

from types import SimpleNamespace

from agent.intent import parse_intent
from agent.registry import AgentRegistry
from agent.runtime.governance import frozen_ranking_mutation_requested
from agent.runtime.task_router import TaskRouter


def test_phase5_route_acceptance_matrix() -> None:
    registry = AgentRegistry()
    router = TaskRouter(registry)

    literature = router.route_scp(
        "查询 MASLD 最新文献", enabled_skill_ids={"literature_research"}
    )
    mechanism = router.route_scp(
        "查询 PPARα 与脂肪酸氧化的机制关系",
        enabled_skill_ids={"mechanism_research"},
    )
    protocol = router.route_scp(
        "生成包含对照组的验证方案", enabled_skill_ids={"validation_protocol"}
    )
    assert literature and literature.capability_id == "literature_search"
    assert mechanism and mechanism.capability_id == "mechanism_relation_search"
    assert protocol and protocol.capability_id == "validation_protocol"

    plugin = registry.plugins["scp-hub"]
    assert plugin.network_policy["writes_selection"] is False
    assert plugin.network_policy["participates_in_ranking"] is False

    nomination = parse_intent("生成 Top10 候选 CSV")
    assert nomination.want_csv is True
    assert nomination.skill_ids == ("masld_nominate",)

    evidence = parse_intent("查询候选 T001 的分子证据卡")
    assert evidence.query_evidence is True
    assert evidence.want_csv is False

    assert frozen_ranking_mutation_requested("根据实时资料重新调整候选优先级")


def test_unified_router_core_rescreen_vs_explain_vs_clarify() -> None:
    from dataclasses import replace

    from agent.intent import (
        extract_ranking_positions,
        ranking_position_subject_fallback,
        ranking_question_fallback,
    )

    registry = AgentRegistry()
    router = TaskRouter(registry)
    session = SimpleNamespace(
        last_result=object(),
        frozen_ranking={"run_id": "mm-x", "top_molecules": [{"molecule_id": "T1"}]},
        last_run_id="mm-x",
        run_history=[{"run_id": "mm-x"}],
        installed_scp_skills={
            "literature_research": {"enabled": True},
        },
    )

    rescreen = parse_intent("忽略之前条件，按默认配置重新筛选")
    assert rescreen.execution_requested is True
    assert rescreen.want_csv is True
    assert rescreen.force_rescreen is True
    assert router.route(rescreen, session).route == "core"

    def as_explain(text: str):
        base = parse_intent(text)
        is_rank, mol_id = ranking_question_fallback(text)
        assert is_rank is True
        return replace(
            base,
            explain_ranking=True,
            wants_tools=False,
            want_csv=False,
            want_pdf=False,
            skill_ids=(),
            execution_requested=False,
            ranking_molecule_id=mol_id,
            ranking_positions=extract_ranking_positions(text),
            ranking_position_subject=ranking_position_subject_fallback(text),
            reason="询问上一轮候选排名原因，不重新筛选或导出",
        )

    explain = as_explain("解释第 3 名的毒性与降脂证据")
    assert explain.explain_ranking is True
    assert explain.ranking_positions == (3,)
    assert router.route(explain, session).route == "explain"

    top5 = as_explain("前 5 名里哪个更适合继续推进？")
    assert top5.explain_ranking is True
    assert top5.ranking_positions == (1, 2, 3, 4, 5)
    assert router.route(top5, session).route == "explain"

    empty = SimpleNamespace(
        last_result=None,
        frozen_ranking=None,
        last_run_id="",
        run_history=[],
        installed_scp_skills={"literature_research": {"enabled": True}},
    )
    assert router.route(explain, empty).route == "clarify"

    declared = router.route_scp(
        "查询 MASLD 最新文献", enabled_skill_ids={"literature_research"}
    )
    assert declared and declared.capability_id == "literature_search"
