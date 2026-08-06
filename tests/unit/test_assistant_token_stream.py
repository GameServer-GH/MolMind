"""Token-level assistant streaming: ephemeral deltas + durable final assistant."""

from __future__ import annotations

from typing import Iterator

from agent.memory.models import AgentSession
from agent.runtime.loop import AgentRuntime
from tests.unit.agent_test_support import MemRunStore


def test_pure_chat_emits_assistant_deltas_then_final(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.runtime.loop.llm_json_decision",
        lambda **kwargs: (kwargs["default"], "llm_not_ready"),
    )
    monkeypatch.setattr(
        "agent.runtime.loop.llm_json_object",
        lambda **kwargs: (kwargs["default"], "llm_not_ready"),
    )
    monkeypatch.setattr(
        "agent.runtime.loop.llm_plan_request",
        lambda **_kwargs: (None, "llm_not_ready"),
    )

    runtime = AgentRuntime(store=MemRunStore())

    def _fake_stream(_session: AgentSession, _text: str) -> Iterator[str]:
        yield "你"
        yield "好"
        yield "，MolMind"

    monkeypatch.setattr(runtime, "_llm_chat_reply_stream", _fake_stream)

    session = AgentSession(session_id="s-stream-chat")
    events = list(runtime.handle_message(session, "你好，介绍一下你自己"))

    types = [e.get("type") for e in events]
    assert "assistant_delta" in types
    assert types.count("assistant_delta") == 3
    deltas = [e.get("delta") for e in events if e.get("type") == "assistant_delta"]
    assert "".join(deltas) == "你好，MolMind"
    assert all(
        e.get("live_only") is True
        for e in events
        if e.get("type") == "assistant_delta"
    )

    # Deltas are live-only and must not be written into the durable event log.
    durable_types = [e.get("type") for e in runtime.store._events]
    assert "assistant_delta" not in durable_types
    assert "assistant" in durable_types

    assistant = next(e for e in events if e.get("type") == "assistant")
    assert assistant.get("text") == "你好，MolMind"
    assert types.index("assistant_delta") < types.index("assistant")
    assert "done" in types
    assert session.messages[-1]["role"] == "assistant"
    assert session.messages[-1]["text"] == "你好，MolMind"
