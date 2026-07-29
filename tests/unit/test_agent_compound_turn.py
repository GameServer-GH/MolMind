from __future__ import annotations

import threading
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from agent.intent import extract_companion_text, parse_intent
from agent.memory import FileRunStore
from agent.runtime.loop import AgentRuntime
from agent.runtime.verification import verify_assistant_claims


@dataclass
class _QueryResult:
    ok: bool = True
    error_code: str = ""
    message: str = "已找到本地证据；主榜未修改。"
    card: dict[str, Any] = field(
        default_factory=lambda: {"status": "hit", "summary": "本地证据命中"}
    )
    degraded_channels: list[str] = field(default_factory=list)
    identity: dict[str, Any] = field(
        default_factory=lambda: {
            "molecule_id": "T001",
            "match_type": "exact_identity",
        }
    )


def _runtime(tmp_path) -> tuple[AgentRuntime, Any, Any]:
    root = tmp_path / "agent_runs"
    runtime = AgentRuntime(store=FileRunStore(root=root))
    session = runtime.create_session()
    session.last_result = SimpleNamespace(scored_molecules=[])
    session.last_run_id = "run-1"
    session.last_selection_sha256 = "selection-stable"
    session.last_molecule_index = {
        "T001": [
            {
                "molecule_id": "T001",
                "inchikey": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
                "smiles": "CCO",
            }
        ]
    }
    return runtime, session, root


def test_companion_text_survives_explicit_mention() -> None:
    text = "调用 /tool:query_evidence T001，并解释证据卡是什么"
    intent = parse_intent(text)

    assert extract_companion_text(text) == "解释证据卡是什么"
    assert intent.companion_text == "解释证据卡是什么"
    assert intent.mentions[0].id == "query_evidence"


def test_companion_extractor_does_not_turn_tool_args_into_chat() -> None:
    assert extract_companion_text("/skill:masld_explain T001") == ""
    assert extract_companion_text("试用 /tool:score_and_rank") == ""
    assert (
        extract_companion_text("介绍 @skill:masld_nominate，它是否会改榜")
        == "它是否会改榜"
    )
    assert (
        extract_companion_text(
            "调用 /tool:query_evidence 查询 Top1，同时用一句话解释代理评分和实验结论的区别"
        )
        == "用一句话解释代理评分和实验结论的区别"
    )


def test_compound_evidence_rank_reference_uses_frozen_molecule(
    monkeypatch, tmp_path
) -> None:
    import plugins.molmind_core.tools.scientific as scientific_tools

    calls: list[dict[str, Any]] = []
    runtime, session, _root = _runtime(tmp_path)
    session.last_result = SimpleNamespace(
        run_id="run-top",
        selection_sha256="selection-stable",
        top_molecules=[SimpleNamespace(molecule_id="T19959")],
        reserve_molecules=[],
        scored_molecules=[],
    )
    session.last_molecule_index = {
        "T19959": [{"molecule_id": "T19959", "smiles": "CCO"}]
    }

    def fake_query(**kwargs):
        calls.append(kwargs)
        return _QueryResult(
            identity={"molecule_id": kwargs["molecule_id"]},
            message="已读取 T19959 的本地证据；主榜未修改。",
        )

    monkeypatch.setattr(scientific_tools, "run_query_evidence", fake_query)
    monkeypatch.setattr(
        runtime,
        "_llm_chat_reply",
        lambda _session, prompt: (
            "代理评分是计算优先级，实验结论来自真实测量。"
            if "代理评分" in prompt
            else ""
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_decide_loop_after_observations",
        lambda **_kwargs: ("final", "all_tasks_satisfied"),
    )

    events = list(
        runtime.handle_message(
            session,
            "调用 /tool:query_evidence 查询 Top1，同时用一句话解释代理评分和实验结论的区别",
        )
    )

    assert calls and calls[0]["molecule_id"] == "T19959"
    start = next(event for event in events if event.get("type") == "tool_start")
    assert start["args"]["molecule_id"] == "T19959"
    assert session.last_selection_sha256 == "selection-stable"
    memory = session.working_memory[-1]
    assert memory["tasks"][1]["text"] == "用一句话解释代理评分和实验结论的区别"
    assert "T19699" not in next(
        event["text"] for event in events if event.get("type") == "assistant"
    )


def test_compound_turn_runs_branches_in_parallel_merges_and_remembers(
    monkeypatch, tmp_path
) -> None:
    import plugins.molmind_core.tools.scientific as scientific_tools

    runtime, session, root = _runtime(tmp_path)
    chat_started = threading.Event()

    def fake_chat(_session, text):
        assert text == "解释证据卡是什么"
        chat_started.set()
        return "证据卡汇总来源、查询状态与声明边界。"

    def fake_query(**kwargs):
        # The chat branch must have started before the tool branch completes.
        assert chat_started.wait(timeout=2.0)
        kwargs["event_sink"](
            {
                "type": "local_hit",
                "provider": "snapshot",
                "status": "hit",
                "count": 1,
            }
        )
        return _QueryResult()

    monkeypatch.setattr(runtime, "_llm_chat_reply", fake_chat)
    monkeypatch.setattr(
        runtime,
        "_decide_loop_after_observations",
        lambda **_kwargs: ("final", "all_tasks_satisfied"),
    )
    monkeypatch.setattr(
        runtime,
        "_merge_compound_reply",
        lambda **_kwargs: "已完成证据查询；证据卡用于汇总来源、状态与声明边界。",
    )
    monkeypatch.setattr(
        scientific_tools,
        "run_query_evidence",
        fake_query,
        raising=False,
    )

    events = list(
        runtime.handle_message(
            session,
            "调用 /tool:query_evidence T001，并解释证据卡是什么",
        )
    )

    assert any(event.get("tool") == "query_evidence" for event in events)
    assert [event["decision"] for event in events if event.get("type") == "loop_decision"] == [
        "final"
    ]
    assistant_events = [
        event for event in events if event.get("type") == "assistant"
    ]
    assert [event["text"] for event in assistant_events] == [
        "已完成证据查询；证据卡用于汇总来源、状态与声明边界。"
    ]
    assert [message["role"] for message in session.messages] == ["user", "assistant"]

    memory = session.working_memory[-1]
    assert memory["decision"] == "final"
    assert memory["tasks"][1]["text"] == "解释证据卡是什么"
    assert memory["tool_calls"][0]["tool"] == "query_evidence"
    assert memory["tool_calls"][0]["status"] == "succeeded"

    loaded = FileRunStore(root=root).get(session.session_id)
    assert loaded is not None
    assert loaded.working_memory[-1]["decision"] == "final"
    assert loaded.working_memory[-1]["tool_calls"][0]["tool"] == "query_evidence"
    graph = session.plan_history[-1]
    assert [task["task_id"] for task in graph["steps"]] == [
        "mention",
        "conversation",
        "synthesis",
    ]
    assert [task["status"] for task in graph["steps"]] == [
        "succeeded",
        "succeeded",
        "succeeded",
    ]


def test_compound_loop_stops_repeated_continue_decision(
    monkeypatch, tmp_path
) -> None:
    runtime, session, _root = _runtime(tmp_path)
    monkeypatch.setattr(
        runtime,
        "_classify_mention_action",
        lambda _text, _mentions: ("introduce", "test"),
    )
    monkeypatch.setattr(
        runtime,
        "_llm_chat_reply",
        lambda _session, _text: "不会改榜。",
    )
    monkeypatch.setattr(
        runtime,
        "_decide_loop_after_observations",
        lambda **_kwargs: ("continue", "retry_synthesis"),
    )
    monkeypatch.setattr(
        runtime,
        "_merge_compound_reply",
        lambda **_kwargs: "点选技能只负责编排，LLM 不直接改榜。",
    )

    events = list(
        runtime.handle_message(
            session,
            "介绍 @skill:masld_nominate，它是否会改榜",
        )
    )

    decisions = [
        event for event in events if event.get("type") == "loop_decision"
    ]
    assert [event["decision"] for event in decisions] == [
        "continue",
        "continue",
        "final",
    ]
    assert "loop_stalled" in decisions[-1]["reason"]
    assert len(session.working_memory) == 3
    assert session.working_memory[-1]["decision"] == "final"


def test_evidence_completion_claim_uses_current_turn_memory_only(tmp_path) -> None:
    _runtime_obj, session, _root = _runtime(tmp_path)
    session.working_memory = [
        {
            "turn_id": "old",
            "tool_calls": [
                {"tool": "query_evidence", "status": "succeeded"}
            ],
        },
        {
            "turn_id": "current",
            "tool_calls": [
                {"tool": "query_evidence", "status": "failed"}
            ],
        },
    ]

    violations = verify_assistant_claims(session, "已完成证据查询。")

    assert [violation.code for violation in violations] == [
        "completion_without_evidence"
    ]


def test_compound_synthesis_cannot_hide_degraded_evidence_channel(tmp_path) -> None:
    runtime, _session, _root = _runtime(tmp_path)

    reply = runtime._append_degraded_disclosure(
        "查询完成：T19959 命中 4 条证据。",
        [
            {
                "tool": "query_evidence",
                "status": "succeeded",
                "observation": {
                    "digest": {
                        "degraded_channels": ["evidence_provenance_incomplete"]
                    }
                },
            }
        ],
    )

    assert "evidence_provenance_incomplete" in reply
    assert "来源溯源字段不完整" in reply
    assert "不代表没有证据" in reply
