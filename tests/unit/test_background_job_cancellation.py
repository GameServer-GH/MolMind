from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from plugins.molmind_core.scientific.mechanism import jobs as mechanism_jobs
from plugins.scp_hub.jobs import SCPJobManager


def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached before timeout")


def test_mechanism_job_discards_payload_after_cancel(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_markdown(*_args, **_kwargs):
        started.set()
        release.wait(timeout=2)
        return "late markdown"

    monkeypatch.setattr(mechanism_jobs, "build_mechanism_markdown", slow_markdown)
    job_id = mechanism_jobs.start_mechanism_job(
        [object()],
        llm_cfg={},
        mark_degraded=None,
        run_context={"agent_run_id": "agent-run-1"},
    )
    assert started.wait(timeout=1)
    assert mechanism_jobs.cancel_job(job_id, reason="user_guidance")
    release.set()
    _wait_for(lambda: mechanism_jobs.get_job(job_id)["status"] == "cancelled")
    job = mechanism_jobs.get_job(job_id)
    assert job is not None
    assert job["mechanism_md"] == ""
    assert job["mechanism_pdf_base64"] == ""


def test_scp_job_discards_late_result_after_run_cancel() -> None:
    manager = SCPJobManager(max_workers=1)
    started = threading.Event()
    release = threading.Event()

    def slow_call():
        started.set()
        release.wait(timeout=2)
        return SimpleNamespace(content=[], status="hit")

    submitted = manager.submit(
        slow_call,
        session_id="session-1",
        skill_id="skill-1",
        tool_id="tool-1",
        run_id="run-1",
    )
    assert started.wait(timeout=1)
    assert manager.cancel_for_run(session_id="session-1", run_id="run-1") == [
        submitted["job_id"]
    ]
    release.set()
    _wait_for(
        lambda: manager.get(submitted["job_id"], session_id="session-1")["status"]
        == "cancelled"
    )
    job = manager.get(submitted["job_id"], session_id="session-1")
    assert job is not None
    assert job["result"] is None
