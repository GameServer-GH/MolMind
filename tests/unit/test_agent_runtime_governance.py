from __future__ import annotations

from types import MethodType
from typing import Any

import pytest

from agent.memory import FileRunStore
from agent.registry import AgentRegistry
from agent.registry.models import ToolSpec
from agent.runtime.governance import ToolGovernance
from agent.runtime.loop import AgentRuntime
from agent.runtime.task_graph import TaskGraph


def _runtime(tmp_path, *, profile_id: str = "competition_masld"):
    runtime = AgentRuntime(store=FileRunStore(root=tmp_path / "agent_runs"))
    # Keep tests isolated from the process-cached production registry.
    runtime.registry = AgentRegistry()
    runtime._governance = ToolGovernance(runtime.registry)
    session = runtime.create_session(profile_id=profile_id)
    return runtime, session


def _install_probe_tool(
    runtime: AgentRuntime,
    *,
    tool_id: str,
    confirmation_required: bool = False,
    large_digest: bool = False,
) -> None:
    runtime.registry.tools[tool_id] = ToolSpec(
        tool_id=tool_id,
        plugin_id="molmind-core",
        title=tool_id,
        input_schema={
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "integer"}},
        },
        idempotent=True,
        confirmation_required=confirmation_required,
    )

    def execute(self, session, *, value: int):
        yield self._emit(
            session,
            {
                "type": "tool_start",
                "tool": tool_id,
                "plugin": "molmind-core",
                "args": {"value": value},
            },
        )
        digest: dict[str, Any] = {"value": value}
        if large_digest:
            digest["payload"] = "x" * 20_000
        yield self._emit(
            session,
            {
                "type": "tool_end",
                "tool": tool_id,
                "ok": True,
                "digest": digest,
            },
        )

    setattr(runtime, f"_execute_{tool_id}", MethodType(execute, runtime))


def test_governance_rejects_invalid_schema_before_adapter(monkeypatch, tmp_path) -> None:
    runtime, session = _runtime(tmp_path)
    session.sdf_bytes = b"fixture"
    called = False

    def must_not_run(*_args, **_kwargs):
        nonlocal called
        called = True
        yield {}

    monkeypatch.setattr(runtime, "_execute_score_and_rank", must_not_run)
    events = list(
        runtime._execute_tool_adapter(
            session,
            "score_and_rank",
            {"top_n": 0},
        )
    )

    assert called is False
    denied = next(event for event in events if event["type"] == "governance_denied")
    assert denied["code"] == "invalid_args"
    end = next(event for event in events if event["type"] == "tool_end")
    assert end["observation"]["status"] == "denied"
    assert end["observation"]["error"]["code"] == "invalid_args"


def test_governance_rejects_missing_precondition(tmp_path) -> None:
    runtime, session = _runtime(tmp_path)

    events = list(
        runtime._execute_tool_adapter(
            session,
            "export_nomination",
            {"tier": "primary"},
        )
    )

    denied = next(event for event in events if event["type"] == "governance_denied")
    assert denied["code"] == "missing_precondition"
    assert "frozen_result" in denied["detail"]
    assert not any(event.get("type") == "tool_start" for event in events)


def test_profile_tool_call_budget_is_a_hard_runtime_limit(tmp_path) -> None:
    runtime, session = _runtime(tmp_path, profile_id="minimal")
    _install_probe_tool(runtime, tool_id="budget_probe")

    events: list[dict[str, Any]] = []
    for value in range(5):
        events.extend(
            runtime._execute_tool_adapter(
                session,
                "budget_probe",
                {"value": value},
            )
        )

    successes = [
        event
        for event in events
        if event.get("type") == "tool_end" and event.get("ok") is True
    ]
    assert len(successes) == 4
    denied = [
        event for event in events if event.get("type") == "governance_denied"
    ]
    assert denied[-1]["code"] == "max_tool_calls_exceeded"
    assert session.agent_run_state is not None
    assert session.agent_run_state["tool_calls"] == 4
    assert session.agent_run_state["stop_reason"] == "max_tool_calls_exceeded"


def test_approval_is_bound_to_exact_tool_args_and_consumed_once(tmp_path) -> None:
    runtime, session = _runtime(tmp_path)
    _install_probe_tool(
        runtime,
        tool_id="review_probe",
        confirmation_required=True,
    )
    approval = runtime.grant_tool_approval(
        session,
        tool_id="review_probe",
        args={"value": 1},
    )

    wrong_args = list(
        runtime._execute_tool_adapter(
            session,
            "review_probe",
            {"value": 2},
        )
    )
    assert next(
        event for event in wrong_args if event["type"] == "governance_denied"
    )["code"] == "approval_required"
    assert approval["used_at"] is None

    approved = list(
        runtime._execute_tool_adapter(
            session,
            "review_probe",
            {"value": 1},
        )
    )
    assert any(
        event.get("type") == "tool_end" and event.get("ok") is True
        for event in approved
    )
    assert approval["used_at"] is not None

    replay = list(
        runtime._execute_tool_adapter(
            session,
            "review_probe",
            {"value": 1},
        )
    )
    assert next(
        event for event in replay if event["type"] == "governance_denied"
    )["code"] == "approval_required"


def test_observation_is_compacted_remembered_and_run_state_persisted(tmp_path) -> None:
    runtime, session = _runtime(tmp_path)
    _install_probe_tool(
        runtime,
        tool_id="observation_probe",
        large_digest=True,
    )

    events = list(
        runtime._execute_tool_adapter(
            session,
            "observation_probe",
            {"value": 7},
        )
    )
    end = next(event for event in events if event["type"] == "tool_end")
    observation = end["observation"]
    assert observation["status"] == "succeeded"
    assert observation["digest"]["compacted"] is True
    assert observation["digest"]["original_chars"] > 12_000
    assert "payload" not in observation["digest"]

    memory = session.working_memory[-1]
    assert memory["tool_calls"][0]["tool"] == "observation_probe"
    assert memory["tool_calls"][0]["status"] == "succeeded"
    runtime._emit(session, {"type": "done"})
    runtime.store.persist(session)

    loaded = FileRunStore(root=tmp_path / "agent_runs").get(session.session_id)
    assert loaded is not None
    assert loaded.agent_run_state is not None
    assert loaded.agent_run_state["status"] == "completed"
    assert loaded.agent_run_state["tool_calls"] == 1


def test_task_graph_exposes_parallel_ready_tasks_and_dependency_gate() -> None:
    graph = TaskGraph.from_steps(
        goal="查询并解释",
        steps=[
            {
                "task_id": "query",
                "kind": "tool",
                "tool": "query_evidence",
                "depends_on": [],
            },
            {
                "task_id": "explain",
                "kind": "conversation",
                "depends_on": [],
            },
            {
                "task_id": "merge",
                "kind": "synthesis",
                "depends_on": ["query", "explain"],
            },
        ],
    )

    assert [task.task_id for task in graph.ready_tasks()] == ["query", "explain"]
    graph.mark_running("query")
    graph.mark_terminal("query", status="succeeded")
    assert [task.task_id for task in graph.ready_tasks()] == ["explain"]
    graph.mark_running("explain")
    graph.mark_terminal("explain", status="succeeded")
    assert [task.task_id for task in graph.ready_tasks()] == ["merge"]


def test_task_graph_rejects_cycles() -> None:
    with pytest.raises(ValueError, match="循环依赖"):
        TaskGraph.from_steps(
            goal="invalid",
            steps=[
                {"task_id": "a", "depends_on": ["b"]},
                {"task_id": "b", "depends_on": ["a"]},
            ],
        )


def test_approval_api_returns_non_secret_exact_binding(
    monkeypatch,
    tmp_path,
) -> None:
    from fastapi.testclient import TestClient

    from agent.runtime import loop as loop_mod
    from apps.api.app import app

    runtime, session = _runtime(tmp_path)
    _install_probe_tool(
        runtime,
        tool_id="api_review_probe",
        confirmation_required=True,
    )
    monkeypatch.setattr(loop_mod, "_RUNTIME", runtime)
    client = TestClient(app)

    response = client.post(
        f"/api/agent/sessions/{session.session_id}/approvals",
        json={
            "tool_id": "api_review_probe",
            "args": {"value": 9},
            "ttl_sec": 120,
        },
    )

    assert response.status_code == 200
    approval = response.json()["approval"]
    assert approval["tool_id"] == "api_review_probe"
    assert len(approval["args_hash"]) == 64
    assert "args" not in approval
    snapshot = client.get(f"/api/agent/sessions/{session.session_id}").json()
    assert snapshot["approvals"][0]["args_hash"] == approval["args_hash"]
