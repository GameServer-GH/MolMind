from agent.memory import FileRunStore
from agent.runtime.loop import AgentRuntime
from agent.runtime.task_router import TaskRoute


def _route(capability_id: str, skill_id: str, tool_id: str) -> TaskRoute:
    return TaskRoute(
        route="scp",
        capability_id=capability_id,
        skill_id=skill_id,
        tool_id=tool_id,
        label=capability_id,
        arguments={"query": capability_id},
        reason="test",
    )


def test_protocol_is_skipped_when_upstream_evidence_is_not_validated(
    monkeypatch, tmp_path
) -> None:
    runtime = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    session = runtime.create_session(client_id="multi_capability_test_0001")
    routes = [
        _route("mechanism_relation_search", "mechanism_research", "mechanism"),
        _route("literature_search", "literature_research", "literature"),
        _route("validation_protocol", "validation_protocol", "protocol"),
    ]
    executed: list[str] = []

    def fake_run(_session, *, route, **_kwargs):
        if False:
            yield {}
        executed.append(route.capability_id)
        relevant = route.capability_id == "literature_search"
        return {
            "route": route,
            "ok": True,
            "relevant": relevant,
            "values": ["validated literature"] if relevant else [],
            "digest": {"response_hash": f"sha256:{route.capability_id}"},
            "claim_scopes": runtime.task_router.claim_scopes(route.capability_id),
            "calls": [],
        }

    monkeypatch.setattr(runtime, "_run_scp_route", fake_run)
    monkeypatch.setattr(
        runtime,
        "_synthesize_scp_multi_reply",
        lambda **_kwargs: "safe multi-capability reply",
    )
    events = list(
        runtime._run_scp_multi_routes(
            session,
            original_question="机制、文献和实验方案",
            evidence_question="机制、文献和实验方案",
            routes=routes,
            enabled_skill_ids={
                "mechanism_research",
                "literature_research",
                "validation_protocol",
            },
            report_cache=False,
        )
    )

    assert executed == ["mechanism_relation_search", "literature_search"]
    skipped = next(
        event
        for event in events
        if event.get("type") == "task_end" and event.get("task_id") == "scp-3"
    )
    assert skipped["status"] == "skipped"
    assert skipped["observation"]["reason"] == "upstream_evidence_not_validated"
    assert next(event for event in events if event.get("type") == "assistant")[
        "text"
    ] == "safe multi-capability reply"


def test_scp_followup_and_repeat_detection_are_distinct(tmp_path) -> None:
    runtime = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    session = runtime.create_session(client_id="scp_followup_test_0001")
    session.messages = [
        {"role": "user", "text": "查询 PPARα 在 MASLD 肝脏脂质代谢中的作用机制"},
        {"role": "assistant", "text": "证据不足，未通过相关性校验。"},
        {"role": "user", "text": "请重新执行相同的机制和文献查询，并检查缓存"},
    ]

    assert runtime._scp_repeat_requested(session.messages[-1]["text"])
    assert not runtime._scp_history_summary_requested(session.messages[-1]["text"])
    assert runtime._scp_previous_scientific_question(session).startswith("查询 PPARα")
    assert "直接机制" in runtime._scp_history_summary_reply(session)
    repeated = runtime.task_router.route_scp_tasks(
        session.messages[-1]["text"],
        enabled_skill_ids={
            "mechanism_research",
            "literature_research",
            "validation_protocol",
        },
    )
    assert [route.capability_id for route in repeated] == [
        "mechanism_relation_search",
        "literature_search",
    ]
