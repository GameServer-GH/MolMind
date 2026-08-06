"""Regression: agent_events seq must never reuse a primary key after a stale reload."""

from __future__ import annotations

from agent.memory import FileRunStore
from agent.memory.models import AgentSession


def test_copy_session_state_does_not_regress_event_seq(tmp_path) -> None:
    store = FileRunStore(root=tmp_path / "runs")
    session = store.create(client_id="evt-seq-copy")
    first = store.append_event(session, {"type": "plan", "steps": ["理解问题"]})
    assert first["seq"] == 1
    assert session.event_seq == 1

    stale = AgentSession(session_id=session.session_id, event_seq=0)
    store._copy_session_state(session, stale)
    assert session.event_seq == 1

    second = store.append_event(session, {"type": "thinking", "text": "准备回复"})
    assert second["seq"] == 2
    assert [event["seq"] for event in store.read_events(session.session_id)] == [1, 2]


def test_append_event_recovers_when_memory_event_seq_lags_events(tmp_path) -> None:
    store = FileRunStore(root=tmp_path / "runs")
    session = store.create(client_id="evt-seq-lag")
    first = store.append_event(session, {"type": "agent_plan", "goal": "你", "action": "chat"})
    assert first["seq"] == 1

    # Reproduce the production failure mode: memory counter was rolled back
    # behind durable agent_events rows, then the next emit reused seq N.
    session.event_seq = 0
    second = store.append_event(session, {"type": "plan", "steps": ["理解问题", "生成对话回复"]})
    assert second["seq"] == 2
    third = store.append_event(session, {"type": "thinking", "text": "识别为一般问答"})
    assert third["seq"] == 3
    assert [event["seq"] for event in store.read_events(session.session_id)] == [1, 2, 3]
    assert session.event_seq == 3
