from __future__ import annotations

from types import SimpleNamespace

from agent.registry import AgentRegistry
from agent.runtime.planning import AgentPlan, plan_for_skills, session_capabilities


def test_skill_contracts_compile_ordered_nomination_plan() -> None:
    registry = AgentRegistry()
    session = SimpleNamespace(sdf_bytes=b"sdf", last_result=None, last_mechanism_job_id="")

    plan, diagnostics = plan_for_skills(
        goal="生成候选清单",
        action="execute",
        skill_ids=["masld_nominate"],
        skills=registry.skills,
        tools=registry.tools,
        capabilities=session_capabilities(session),
    )

    assert diagnostics == []
    assert [step.tool_id for step in plan.steps] == [
        "parse_sdf",
        "score_and_rank",
        "export_nomination",
        "build_evidence_card",
    ]
    assert plan.expected_artifacts == ("frozen_result", "candidate_csv")


def test_plan_rejects_tool_when_declared_precondition_is_missing() -> None:
    registry = AgentRegistry()
    session = SimpleNamespace(sdf_bytes=None, last_result=None, last_mechanism_job_id="")

    plan, diagnostics = plan_for_skills(
        goal="生成候选清单",
        action="execute",
        skill_ids=["masld_nominate"],
        skills=registry.skills,
        tools=registry.tools,
        capabilities=session_capabilities(session),
    )

    assert plan.steps == ()
    assert any(item.startswith("missing_precondition:parse_sdf:sdf") for item in diagnostics)


def test_runtime_uses_registry_backed_llm_plan_before_legacy_classifier(
    monkeypatch, tmp_path
) -> None:
    from agent.memory import FileRunStore
    from agent.runtime.loop import AgentRuntime

    monkeypatch.setattr(
        "agent.runtime.loop.llm_plan_request",
        lambda **_kwargs: (
            AgentPlan(goal="解释已有结果", action="explain", rationale="冻结结果追问"),
            "llm",
        ),
    )
    runtime = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    session = runtime.create_session()
    intent = __import__("agent.intent", fromlist=["parse_intent"]).parse_intent(
        "解释一下 Top10"
    )

    action, why = runtime._classify_request_action(session, "解释一下 Top10", intent)
    assert action == "explain_ranking"
    assert why.startswith("llm;")


def test_clarify_plan_persists_unexecutable_goal(monkeypatch, tmp_path) -> None:
    from agent.memory import FileRunStore
    from agent.runtime.loop import AgentRuntime

    monkeypatch.setattr(
        "agent.runtime.loop.llm_plan_request",
        lambda **_kwargs: (
            AgentPlan(
                goal="按 PAINS 与 PPARα 偏好筛选",
                action="clarify",
                rationale="当前工具契约没有这些参数",
            ),
            "llm",
        ),
    )
    runtime = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    session = runtime.create_session()
    intent = __import__("agent.intent", fromlist=["parse_intent"]).parse_intent("排除 PAINS")

    action, _ = runtime._classify_request_action(session, "排除 PAINS", intent)
    assert action == "chat"
    assert session.pending_goal is not None
    assert session.pending_goal["reason"] == "tool_contract_missing_parameters"


def test_runtime_persists_plan_step_observations(tmp_path) -> None:
    from agent.memory import FileRunStore
    from agent.runtime.loop import AgentRuntime

    runtime = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    session = runtime.create_session()
    runtime._emit(
        session,
        {
            "type": "agent_plan",
            "goal": "生成候选清单",
            "action": "execute",
            "steps": [{"tool": "score_and_rank", "args": {"top_n": 10}}],
            "expected_artifacts": ["candidate_csv"],
            "diagnostics": [],
        },
    )
    runtime._emit(session, {"type": "tool_start", "tool": "score_and_rank"})
    runtime._emit(
        session,
        {
            "type": "tool_end",
            "tool": "score_and_rank",
            "ok": True,
            "digest": {"run_id": "mm-test"},
        },
    )
    runtime._emit(session, {"type": "done"})

    plan = session.plan_history[-1]
    assert plan["status"] == "completed"
    assert plan["steps"][0]["status"] == "succeeded"
    assert plan["steps"][0]["observation"]["digest"]["run_id"] == "mm-test"


def test_runtime_does_not_mark_unstarted_planned_steps_completed(tmp_path) -> None:
    from agent.memory import FileRunStore
    from agent.runtime.loop import AgentRuntime

    runtime = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    session = runtime.create_session()
    runtime._emit(
        session,
        {
            "type": "agent_plan",
            "goal": "生成候选清单",
            "action": "execute",
            "steps": [
                {"tool": "score_and_rank", "args": {"top_n": 10}},
                {"tool": "export_nomination", "args": {"tier": "primary"}},
            ],
            "expected_artifacts": ["frozen_result", "candidate_csv"],
            "diagnostics": [],
        },
    )
    runtime._emit(session, {"type": "done"})

    plan = session.plan_history[-1]
    assert plan["status"] == "incomplete"
    assert [step["status"] for step in plan["steps"]] == [
        "not_executed",
        "not_executed",
    ]
    assert all(
        step["observation"]["reason"] == "stream_ended_before_execution"
        for step in plan["steps"]
    )
