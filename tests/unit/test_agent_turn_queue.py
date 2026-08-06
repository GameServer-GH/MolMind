from __future__ import annotations

import threading
import types

import pytest
from fastapi.testclient import TestClient

from agent.memory import FileRunStore
from agent.runtime.loop import AgentRuntime, TurnQueueFullError
from agent.runtime.context import build_context_window
from apps.api.app import app


def make_runtime(tmp_path) -> tuple[AgentRuntime, object]:
    runtime = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    session = runtime.create_session(client_id="browser_turn_queue_0001")
    return runtime, session


def test_turn_queue_is_durable_fifo_and_capped_at_three(tmp_path) -> None:
    runtime, session = make_runtime(tmp_path)
    first = runtime.submit_session_turn(session.session_id, "first")
    assert first["disposition"] == "started"

    for index in range(1, 4):
        queued = runtime.submit_session_turn(session.session_id, f"queued-{index}")
        assert queued["disposition"] == "queued"
        assert queued["queue_position"] == index

    with pytest.raises(TurnQueueFullError):
        runtime.submit_session_turn(session.session_id, "queued-4")

    reloaded = FileRunStore(root=tmp_path / "runs").get(session.session_id)
    assert reloaded is not None
    assert [item["text"] for item in reloaded.pending_turns] == [
        "queued-1",
        "queued-2",
        "queued-3",
    ]

    assert session.active_run is not None
    session.active_run["status"] = "succeeded"
    runtime.store.persist(session)
    promoted = runtime.activate_next_queued_turn(session.session_id)
    assert promoted is not None
    assert promoted["input"]["text"] == "queued-1"


def test_explicit_queue_mode_enqueues_even_when_idle(tmp_path) -> None:
    """Browser may queue while the previous turn's streaming UI is still draining."""
    runtime, session = make_runtime(tmp_path)
    first = runtime.submit_session_turn(session.session_id, "first")
    assert first["disposition"] == "started"
    session.active_run["status"] = "succeeded"
    runtime.store.persist(session)

    queued = runtime.submit_session_turn(
        session.session_id,
        "during-typewriter",
        mode="queue",
        idempotency_key="queue-while-idle",
    )
    assert queued["disposition"] == "queued"
    assert queued["text"] == "during-typewriter"
    assert runtime._normal_queue_size(session) == 1
    assert not runtime._run_is_active(session.active_run)

    promoted = runtime.activate_next_queued_turn(session.session_id)
    assert promoted is not None
    assert promoted["input"]["text"] == "during-typewriter"


def test_idempotency_key_does_not_duplicate_active_or_queued_turn(tmp_path) -> None:
    runtime, session = make_runtime(tmp_path)
    active = runtime.submit_session_turn(
        session.session_id,
        "first",
        idempotency_key="same-active",
    )
    duplicate_active = runtime.submit_session_turn(
        session.session_id,
        "first",
        idempotency_key="same-active",
    )
    assert duplicate_active["duplicate"] is True
    assert duplicate_active["run_id"] == active["run_id"]

    queued = runtime.submit_session_turn(
        session.session_id,
        "next",
        idempotency_key="same-queued",
    )
    duplicate_queued = runtime.submit_session_turn(
        session.session_id,
        "next",
        idempotency_key="same-queued",
    )
    assert duplicate_queued["duplicate"] is True
    assert duplicate_queued["turn_id"] == queued["turn_id"]
    assert runtime._normal_queue_size(session) == 1


def test_cancel_queued_turn_releases_its_attachment(tmp_path) -> None:
    runtime, session = make_runtime(tmp_path)
    runtime.submit_session_turn(session.session_id, "running")
    attachment = runtime.store.stage_attachment(
        session,
        filename="next.sdf",
        content=b"next",
        media_type="chemical/x-mdl-sdfile",
    )
    queued = runtime.submit_session_turn(
        session.session_id,
        "use next",
        attachment_ids=[attachment["attachment_id"]],
    )
    assert session.staged_attachments[attachment["attachment_id"]]["state"] == "queued"
    assert not runtime.store.delete_staged_attachment(session, attachment["attachment_id"])
    assert runtime.cancel_queued_turn(session.session_id, queued["turn_id"])
    assert session.staged_attachments[attachment["attachment_id"]]["state"] == "draft"


def test_queued_turn_can_be_edited_and_reordered(tmp_path) -> None:
    runtime, session = make_runtime(tmp_path)
    runtime.submit_session_turn(session.session_id, "running")
    first = runtime.submit_session_turn(session.session_id, "first")
    second = runtime.submit_session_turn(session.session_id, "second")

    edited = runtime.update_queued_turn(
        session.session_id,
        second["turn_id"],
        text="second edited",
    )
    assert edited is not None
    assert edited["text"] == "second edited"

    ordered = runtime.reorder_queued_turns(
        session.session_id,
        [second["turn_id"], first["turn_id"]],
    )
    assert [item["text"] for item in ordered] == ["second edited", "first"]


def test_staged_attachment_cannot_be_claimed_by_two_turns(tmp_path) -> None:
    runtime, session = make_runtime(tmp_path)
    runtime.submit_session_turn(session.session_id, "running")
    attachment = runtime.store.stage_attachment(
        session,
        filename="only-once.sdf",
        content=b"single owner",
    )
    runtime.submit_session_turn(
        session.session_id,
        "first owner",
        attachment_ids=[attachment["attachment_id"]],
    )
    with pytest.raises(ValueError, match="不可用"):
        runtime.submit_session_turn(
            session.session_id,
            "second owner",
            attachment_ids=[attachment["attachment_id"]],
        )


def test_guidance_requests_interrupt_and_is_promoted_first(tmp_path) -> None:
    runtime, session = make_runtime(tmp_path)
    first = runtime.submit_session_turn(session.session_id, "生成 top10")
    controller = runtime._begin_agent_turn(session, run_id=first["run_id"])
    runtime.submit_session_turn(session.session_id, "later")

    guidance = runtime.submit_session_turn(
        session.session_id,
        "只保留 top5",
        mode="guidance",
    )
    assert guidance["disposition"] == "guidance"
    assert controller.cancel_event.is_set()
    assert session.active_run["status"] == "cancel_requested"
    assert session.pending_turns[0]["kind"] == "guidance"

    session.active_run["status"] = "interrupted"
    runtime.store.persist(session)
    promoted = runtime.activate_next_queued_turn(session.session_id)
    assert promoted is not None
    assert promoted["kind"] == "guidance"
    assert promoted["parent_run_id"] == first["run_id"]
    assert "原任务：生成 top10" in promoted["input"]["text"]
    assert "用户补充指引：只保留 top5" in promoted["input"]["text"]


def test_user_stop_interrupts_without_guidance_turn(tmp_path) -> None:
    runtime, session = make_runtime(tmp_path)
    first = runtime.submit_session_turn(session.session_id, "生成 top10")
    controller = runtime._begin_agent_turn(session, run_id=first["run_id"])
    runtime.submit_session_turn(session.session_id, "later")

    result = runtime.interrupt_session_run(
        session.session_id,
        first["run_id"],
        reason="user_stop",
    )
    assert result["interrupted"] is True
    assert result["reason"] == "user_stop"
    assert controller.cancel_event.is_set()
    assert controller.interrupt_reason == "user_stop"
    assert session.active_run["status"] == "cancel_requested"
    assert session.active_run["interrupt_reason"] == "user_stop"
    assert not any(item.get("kind") == "guidance" for item in session.pending_turns)
    assert len(session.pending_turns) == 1
    assert session.pending_turns[0]["text"] == "later"

def test_concurrent_enqueue_never_exceeds_limit(tmp_path) -> None:
    runtime, session = make_runtime(tmp_path)
    runtime.submit_session_turn(session.session_id, "running")
    accepted: list[str] = []
    rejected: list[str] = []

    def submit(index: int) -> None:
        try:
            runtime.submit_session_turn(session.session_id, f"q-{index}")
            accepted.append(str(index))
        except TurnQueueFullError:
            rejected.append(str(index))

    threads = [threading.Thread(target=submit, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert len(accepted) == 3
    assert len(rejected) == 5
    assert runtime._normal_queue_size(session) == 3


def test_busy_turn_api_queues_lists_and_cancels(monkeypatch, tmp_path) -> None:
    from agent.runtime import loop as loop_mod

    runtime = AgentRuntime(store=FileRunStore(root=tmp_path / "api-runs"))
    loop_mod._RUNTIME = runtime
    client = TestClient(app)
    client.headers.update({"X-MolMind-Client-ID": "browser_turn_queue_api_0001"})
    session_id = client.post("/api/agent/sessions").json()["session_id"]
    runtime.reserve_session_run(session_id, "running")

    staged = client.post(
        f"/api/agent/sessions/{session_id}/turn-attachments",
        files={"file": ("next.sdf", b"next", "chemical/x-mdl-sdfile")},
    )
    assert staged.status_code == 200
    attachment_id = staged.json()["attachment"]["attachment_id"]

    queued = client.post(
        f"/api/agent/sessions/{session_id}/turns",
        json={
            "text": "queued",
            "mode": "queue",
            "attachment_ids": [attachment_id],
            "idempotency_key": "turn-api-1",
        },
    )
    assert queued.status_code == 200
    assert queued.json()["disposition"] == "queued"

    listed = client.get(f"/api/agent/sessions/{session_id}/turns")
    assert listed.json()["queue_count"] == 1
    turn_id = listed.json()["turns"][0]["turn_id"]
    edited = client.patch(
        f"/api/agent/sessions/{session_id}/turns/{turn_id}",
        json={"text": "queued edited"},
    )
    assert edited.status_code == 200
    assert edited.json()["turn"]["text"] == "queued edited"
    cancelled = client.delete(f"/api/agent/sessions/{session_id}/turns/{turn_id}")
    assert cancelled.status_code == 200
    assert cancelled.json()["queue_count"] == 0


def test_guidance_unwinds_before_late_assistant_is_committed(tmp_path) -> None:
    runtime, session = make_runtime(tmp_path)
    reserved = runtime.reserve_session_run(session.session_id, "original")
    started = threading.Event()
    release = threading.Event()
    observed: list[dict] = []

    def slow_message(self, owned_session, text, *, run_id=None):
        self._begin_agent_turn(owned_session, run_id=run_id)
        started.set()
        release.wait(timeout=2)
        yield self._emit(owned_session, {"type": "assistant", "text": "late answer"})
        yield self._emit(owned_session, {"type": "done"})

    runtime.handle_message = types.MethodType(slow_message, runtime)
    worker = threading.Thread(
        target=lambda: observed.extend(
            runtime.handle_reserved_session_message(
                session.session_id,
                reserved["run_id"],
                "original",
            )
        )
    )
    worker.start()
    assert started.wait(timeout=1)
    runtime.submit_session_turn(session.session_id, "new direction", mode="guidance")
    release.set()
    worker.join(timeout=2)

    assert not any(event.get("text") == "late answer" for event in observed)
    assert [event.get("type") for event in observed][-2:] == ["run_interrupted", "done"]
    assert session.active_run is not None
    assert session.active_run["status"] == "interrupted"


def test_guidance_before_worker_start_never_runs_old_goal(tmp_path) -> None:
    runtime, session = make_runtime(tmp_path)
    reserved = runtime.reserve_session_run(session.session_id, "old goal")
    runtime.submit_session_turn(session.session_id, "new guidance", mode="guidance")

    events = list(
        runtime.handle_reserved_session_message(
            session.session_id,
            reserved["run_id"],
            "old goal",
        )
    )
    assert [event.get("type") for event in events][-2:] == ["run_interrupted", "done"]
    assert not any(event.get("type") == "assistant" for event in events)
    assert [message.get("text") for message in session.messages] == ["old goal"]


def test_context_builder_compresses_old_turns_but_keeps_latest_guidance() -> None:
    messages = [
        {"role": "user" if index % 2 == 0 else "assistant", "text": f"old-{index}-" + "x" * 900}
        for index in range(20)
    ]
    window = build_context_window(
        messages=messages,
        working_memory=[{"tool": "query_evidence", "status": "succeeded"}],
        resume_context={
            "original_goal": "生成 top10",
            "latest_guidance": "只保留 top5",
            "parent_run_id": "agent-parent",
        },
        max_input_tokens=2_000,
        reserved_tokens=800,
    )
    assert window.summary is not None
    assert "较早对话压缩摘要" in window.history
    assert "只保留 top5" in window.resume_context
    assert "agent-parent" in window.resume_context


def test_startup_recovery_schedules_persisted_queue(monkeypatch, tmp_path) -> None:
    from agent.runtime import loop as loop_mod
    from apps.api import agent_routes

    runtime, session = make_runtime(tmp_path)
    runtime.submit_session_turn(session.session_id, "old active")
    runtime.submit_session_turn(session.session_id, "persisted next")
    session.active_run["status"] = "interrupted"
    runtime.store.persist(session)
    loop_mod._RUNTIME = runtime
    scheduled: list[str] = []
    monkeypatch.setattr(
        agent_routes,
        "_schedule_pending_if_idle",
        lambda _runtime, session_id: scheduled.append(session_id),
    )

    assert agent_routes.recover_pending_sessions() == 1
    assert scheduled == [session.session_id]
