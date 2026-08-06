from tests.unit.agent_test_support import make_runtime_stub, make_session
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


def test_protocol_is_skipped_when_upstream_evidence_is_not_validated(monkeypatch) -> None:
    runtime = make_runtime_stub()
    session = make_session()
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
    monkeypatch.setattr(
        runtime,
        "_emit",
        lambda _session, event: event,
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


def test_scp_followup_regex_helpers_are_offline_only() -> None:
    """Regex helpers remain for LLM-down fallback; online uses scp_dialog_act."""
    runtime = make_runtime_stub()
    session = make_session(
        messages=[
            {"role": "user", "text": "查询 PPARα 在 MASLD 肝脏脂质代谢中的作用机制"},
            {"role": "assistant", "text": "证据不足，未通过相关性校验。"},
            {"role": "user", "text": "请重新执行相同的机制和文献查询，并检查缓存"},
        ]
    )

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


def test_classify_scp_dialog_act_uses_llm_online(monkeypatch) -> None:
    runtime = make_runtime_stub()
    session = make_session(
        messages=[
            {"role": "user", "text": "查询 PPARα 机制"},
            {"role": "assistant", "text": "证据不足"},
        ]
    )
    monkeypatch.setattr(
        "agent.runtime.loop.llm_json_object",
        lambda **kwargs: (
            {
                "scp_dialog_act": "repeat",
                "report_cache": True,
                "reason": "user asked to rerun with cache audit",
            },
            "ok",
        ),
    )
    act, meta = runtime._classify_scp_dialog_act(
        session, "请重新执行相同的机制和文献查询，并检查缓存"
    )
    assert act == "repeat"
    assert meta["report_cache"] is True
    assert meta["status"] == "ok"


def test_classify_scp_dialog_act_falls_back_offline(monkeypatch) -> None:
    runtime = make_runtime_stub()
    session = make_session()
    monkeypatch.setattr(
        "agent.runtime.loop.llm_json_object",
        lambda **kwargs: (kwargs["default"], "llm_not_ready"),
    )
    act, meta = runtime._classify_scp_dialog_act(
        session, "根据刚才的证据汇总一下结果"
    )
    assert act == "reuse"
    assert meta["status"] == "llm_not_ready"
