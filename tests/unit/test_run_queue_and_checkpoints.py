from __future__ import annotations

import sqlite3
import types
import threading
import os
import uuid

import pytest

from agent.memory import FileRunStore
from agent.registry.models import PluginSpec, ToolSpec
from agent.runtime.governance import ToolGovernance
from agent.runtime.loop import AgentRuntime
from agent.runtime.run_queue import PostgresRunQueue, SQLiteRunQueue


def test_sqlite_queue_claim_is_exclusive_and_enqueue_is_idempotent(tmp_path) -> None:
    path = tmp_path / "queue.sqlite3"
    first = SQLiteRunQueue(path)
    second = SQLiteRunQueue(path)
    job_id = first.enqueue(
        run_id="run-1",
        session_id="session-1",
        payload={"text": "hello", "top_n": 10},
    )
    assert second.enqueue(
        run_id="run-1",
        session_id="session-1",
        payload={"text": "duplicate"},
    ) == job_id

    claimed = first.claim(owner="worker-a", lease_seconds=30)
    assert claimed is not None
    assert claimed.run_id == "run-1"
    assert claimed.payload["text"] == "hello"
    assert second.claim(owner="worker-b", lease_seconds=30) is None
    assert not second.complete(job_id, owner="worker-b")
    assert first.renew(job_id, owner="worker-a", lease_seconds=30)
    assert first.complete(job_id, owner="worker-a")
    assert not first.has_live_run("run-1")


@pytest.mark.skipif(
    not os.environ.get("MOLMIND_TEST_POSTGRES_DSN"),
    reason="PostgreSQL integration DSN not configured",
)
def test_postgres_queue_skip_locked_and_owner_fencing() -> None:
    dsn = str(os.environ["MOLMIND_TEST_POSTGRES_DSN"])
    first = PostgresRunQueue(dsn)
    second = PostgresRunQueue(dsn)
    suffix = uuid.uuid4().hex
    run_id = f"pg-run-{suffix}"
    job_id = first.enqueue(run_id=run_id, session_id="pg-session", payload={"value": 1})
    claimed = first.claim(owner="pg-worker-a", lease_seconds=30)
    while claimed is not None and claimed.run_id != run_id:
        first.complete(claimed.job_id, owner="pg-worker-a")
        claimed = first.claim(owner="pg-worker-a", lease_seconds=30)
    assert claimed is not None
    assert claimed.payload == {"value": 1}
    assert second.claim(owner="pg-worker-b", lease_seconds=30) is None
    assert not second.complete(job_id, owner="pg-worker-b")
    assert first.renew(job_id, owner="pg-worker-a", lease_seconds=30)
    assert first.complete(job_id, owner="pg-worker-a")


def test_expired_lease_is_reclaimed_and_failure_is_retried(tmp_path) -> None:
    path = tmp_path / "queue.sqlite3"
    queue = SQLiteRunQueue(path)
    job_id = queue.enqueue(run_id="run-2", session_id="s", payload={})
    claimed = queue.claim(owner="worker-a", lease_seconds=30)
    assert claimed is not None
    with sqlite3.connect(str(path)) as connection:
        connection.execute(
            "UPDATE agent_run_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE job_id=?",
            (job_id,),
        )
    reclaimed = queue.claim(owner="worker-b", lease_seconds=30)
    assert reclaimed is not None
    assert reclaimed.attempt == 2
    assert queue.fail(job_id, owner="worker-b", error="transient", max_attempts=3)
    with sqlite3.connect(str(path)) as connection:
        connection.execute(
            "UPDATE agent_run_jobs SET available_at='2000-01-01T00:00:00+00:00' WHERE job_id=?",
            (job_id,),
        )
    third = queue.claim(owner="worker-c", lease_seconds=30)
    assert third is not None
    assert third.attempt == 3
    assert queue.fail(job_id, owner="worker-c", error="terminal", max_attempts=3)
    assert queue.claim(owner="worker-d", lease_seconds=30) is None


def test_distributed_cancel_flag_is_visible_to_worker(tmp_path) -> None:
    queue = SQLiteRunQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(run_id="run-cancel", session_id="s", payload={})
    assert queue.request_cancel("run-cancel", reason="user_guidance")
    assert queue.cancel_reason("run-cancel") == "user_guidance"


def test_two_runtime_instances_do_not_overwrite_session_reservation(tmp_path) -> None:
    root = tmp_path / "runs"
    first = AgentRuntime(store=FileRunStore(root=root))
    session = first.create_session(client_id="multi-worker-client-0001")
    second = AgentRuntime(store=FileRunStore(root=root))
    first.store.lease_managed = True
    second.store.lease_managed = True
    barrier = threading.Barrier(2)
    dispositions: list[str] = []

    def submit(runtime: AgentRuntime, text: str) -> None:
        barrier.wait(timeout=2)
        accepted = runtime.submit_session_turn(session.session_id, text)
        dispositions.append(str(accepted["disposition"]))

    threads = [
        threading.Thread(target=submit, args=(first, "from-a")),
        threading.Thread(target=submit, args=(second, "from-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    reloaded = FileRunStore(root=root).get(session.session_id)
    assert reloaded is not None
    assert sorted(dispositions) == ["queued", "started"]
    assert reloaded.active_run is not None
    assert len(reloaded.pending_turns) == 1
    assert {
        str((reloaded.active_run.get("input") or {}).get("text") or ""),
        str(reloaded.pending_turns[0].get("text") or ""),
    } == {"from-a", "from-b"}


def test_tool_checkpoint_is_persisted_and_retry_has_lineage(tmp_path) -> None:
    runtime = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    session = runtime.create_session(client_id="checkpoint-client-0001")
    reserved = runtime.reserve_session_run(session.session_id, "original goal")
    runtime._begin_agent_turn(session, run_id=reserved["run_id"])
    runtime._emit(
        session,
        {"type": "tool_start", "tool": "read_only_tool", "args": {"query": "MASLD"}},
    )
    runtime._emit(
        session,
        {
            "type": "tool_end",
            "tool": "read_only_tool",
            "ok": True,
            "status": "succeeded",
            "digest": {"hits": 3},
        },
    )
    runtime._emit(session, {"type": "done", "status": "succeeded"})

    checkpoint = session.tool_checkpoints[-1]
    assert checkpoint["status"] == "succeeded"
    assert checkpoint["terminal_event"]["digest"] == {"hits": 3}
    assert session.agent_run_history[-1]["run_id"] == reserved["run_id"]

    retry = runtime.retry_session_run(session.session_id, reserved["run_id"])
    assert retry["retry_of_run_id"] == reserved["run_id"]
    assert retry["parent_run_id"] == reserved["run_id"]
    assert retry["resume_context"]["completed_tool_checkpoints"][0]["checkpoint_id"] == checkpoint["checkpoint_id"]


def test_guidance_marks_running_checkpoint_for_exact_reexecution(tmp_path) -> None:
    runtime = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    session = runtime.create_session(client_id="checkpoint-client-0002")
    reserved = runtime.reserve_session_run(session.session_id, "original goal")
    runtime._begin_agent_turn(session, run_id=reserved["run_id"])
    runtime._emit(session, {"type": "tool_start", "tool": "slow_tool", "args": {"x": 1}})

    runtime.submit_session_turn(session.session_id, "change direction", mode="guidance")
    checkpoint = session.tool_checkpoints[-1]
    assert checkpoint["status"] == "interrupted"
    assert checkpoint["retryable"] is True
    assert checkpoint["interrupt_reason"] == "user_guidance"


def test_retry_reuses_successful_read_only_checkpoint_without_reexecution(tmp_path) -> None:
    runtime = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    runtime.registry.plugins["test-checkpoint"] = PluginSpec(
        plugin_id="test-checkpoint",
        title="test",
        builtin=True,
        enabled=True,
    )
    runtime.registry.tools["checkpoint_probe"] = ToolSpec(
        tool_id="checkpoint_probe",
        plugin_id="test-checkpoint",
        title="probe",
        idempotent=True,
        input_schema={
            "type": "object",
            "required": ["query"],
            "additionalProperties": False,
            "properties": {"query": {"type": "string"}},
        },
    )
    runtime._governance = ToolGovernance(runtime.registry)
    calls: list[str] = []

    def execute_probe(self, owned_session, query):
        calls.append(query)
        yield self._emit(
            owned_session,
            {"type": "tool_start", "tool": "checkpoint_probe", "args": {"query": query}},
        )
        yield self._emit(
            owned_session,
            {"type": "tool_end", "tool": "checkpoint_probe", "ok": True, "digest": {"value": 7}},
        )

    runtime._execute_checkpoint_probe = types.MethodType(execute_probe, runtime)
    session = runtime.create_session(client_id="checkpoint-client-0003")
    first = runtime.reserve_session_run(session.session_id, "probe")
    runtime._begin_agent_turn(session, run_id=first["run_id"])
    list(runtime._execute_tool_adapter(session, "checkpoint_probe", {"query": "same"}))
    runtime._emit(session, {"type": "done", "status": "succeeded"})
    retry = runtime.retry_session_run(session.session_id, first["run_id"])
    runtime._begin_agent_turn(session, run_id=retry["run_id"])

    replayed = list(
        runtime._execute_tool_adapter(session, "checkpoint_probe", {"query": "same"})
    )
    assert calls == ["same"]
    assert [event["type"] for event in replayed] == ["tool_start", "tool_end"]
    assert replayed[-1]["checkpoint_reused"] is True
    assert replayed[-1]["digest"] == {"value": 7}
