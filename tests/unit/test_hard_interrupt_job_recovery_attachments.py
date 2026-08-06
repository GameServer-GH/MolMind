"""Unit tests for hard interrupt fencing, job redelivery, and attachment context."""

from __future__ import annotations

import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from agent.memory.attachments import (
    format_attachment_context,
    summarize_attachment_for_context,
)
from agent.runtime.cancellable_call import CallCancelled, run_cancellable, wait_interruptible
from plugins.molmind_core.scientific.mechanism import jobs as mechanism_jobs
from plugins.scp_hub.jobs import SCPJobManager


def test_run_cancellable_returns_promptly_on_cancel() -> None:
    started = threading.Event()
    release = threading.Event()
    cancel = threading.Event()

    def slow() -> str:
        started.set()
        release.wait(timeout=2)
        return "late"

    def invoke() -> None:
        with pytest.raises(CallCancelled):
            run_cancellable(slow, cancel_event=cancel, poll_sec=0.05)

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    assert started.wait(timeout=1)
    cancel.set()
    worker.join(timeout=1)
    assert not worker.is_alive()
    release.set()


def test_wait_interruptible_detects_cancel() -> None:
    cancel = threading.Event()
    threading.Timer(0.1, cancel.set).start()
    assert wait_interruptible(cancel, timeout_sec=2.0, slice_sec=0.05) is True


def test_summarize_text_attachment_includes_excerpt() -> None:
    summary = summarize_attachment_for_context(
        {"filename": "notes.md", "kind": "document", "attachment_id": "a1"},
        b"# Title\nMASLD notes about FXR.",
    )
    assert summary["usable_for_ranking"] is False
    assert "MASLD" in summary["excerpt"]
    text = format_attachment_context([summary])
    assert "notes.md" in text
    assert "不可当作已完成筛选结果" in text


def test_summarize_pdf_attachment_is_metadata_only() -> None:
    summary = summarize_attachment_for_context(
        {"filename": "paper.pdf", "kind": "pdf"},
        b"%PDF-1.4 fake",
    )
    assert summary["excerpt"] == ""
    assert "未解析正文" in summary["note"]


def test_mechanism_resume_skips_completed_markdown(monkeypatch) -> None:
    markdown_calls = {"count": 0}

    def counting_markdown(*_args, **_kwargs):
        markdown_calls["count"] += 1
        return "should-not-run"

    monkeypatch.setattr(mechanism_jobs, "build_mechanism_markdown", counting_markdown)
    monkeypatch.setattr(
        mechanism_jobs,
        "build_mechanism_html",
        lambda *_args, **_kwargs: "<html>should-not-run</html>",
    )
    monkeypatch.setattr(
        mechanism_jobs,
        "html_to_pdf_bytes",
        lambda *_args, **_kwargs: b"%PDF-ok",
    )
    monkeypatch.setattr(mechanism_jobs, "_persist", lambda *_a, **_k: None)
    monkeypatch.setattr(mechanism_jobs, "_raise_if_cancelled", lambda *_a, **_k: None)
    monkeypatch.setattr(mechanism_jobs, "_acquire_runtime_lease", lambda *_a, **_k: "owner")
    monkeypatch.setattr(mechanism_jobs, "_release_runtime_lease", lambda *_a, **_k: None)
    monkeypatch.setattr(
        mechanism_jobs,
        "_blob_store",
        lambda: SimpleNamespace(put=lambda *a, **k: {"blob_id": "b1"}),
    )

    job_id = "resume-job-1"

    def fake_hydrate(target_id: str):
        if target_id != job_id:
            return None
        return {
            "job_id": job_id,
            "status": "recovering",
            "error": "orphaned_after_crash",
            "mechanism_md": "saved markdown",
            "mechanism_html": "<html>saved</html>",
            "mechanism_pdf_base64": "",
            "mechanism_pdf_name": "x.pdf",
            "pdf_renderer": "",
            "pdf_blob_id": "",
            "md_blob_id": "md1",
            "html_blob_id": "html1",
            "stage": "pdf",
            "stages_done": ["markdown", "html"],
            "created_at": "",
            "updated_at": "",
            "run_id": "run-1",
            "agent_run_id": "run-1",
            "cancel_reason": "",
            "session_id": "s1",
            "resume_inputs": {
                "top": [],
                "llm_cfg": {},
                "assumptions": {},
                "run_context": {"session_id": "s1"},
                "mechanism_graphs": [],
                "pdf_name": "x.pdf",
            },
        }

    monkeypatch.setattr(mechanism_jobs, "_hydrate_from_db", fake_hydrate)
    assert mechanism_jobs.resume_mechanism_job(job_id) is True
    deadline = time.time() + 2
    while time.time() < deadline:
        job = mechanism_jobs.get_job(job_id)
        if job and job.get("status") == "ready":
            break
        time.sleep(0.02)
    job = mechanism_jobs.get_job(job_id)
    assert job is not None
    assert job["status"] == "ready"
    assert markdown_calls["count"] == 0
    assert job.get("mechanism_pdf_base64")


def test_cancel_scope_makes_chat_completion_cancellable(monkeypatch) -> None:
    from agent.runtime.cancellable_call import cancel_scope
    from plugins.molmind_core.scientific.mechanism import llm_client

    started = threading.Event()
    release = threading.Event()

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "late"}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            started.set()
            release.wait(timeout=2)
            return FakeResp()

    monkeypatch.setattr(llm_client.httpx, "Client", FakeClient)
    settings = llm_client.LLMSettings(
        enabled=True,
        model="m",
        base_url="http://example.test/v1",
        api_key="k",
        temperature=0.0,
        timeout_sec=5.0,
        max_tokens=64,
        cache_dir=llm_client.DEFAULT_CACHE_DIR,
        use_cache=False,
    )
    cancel = threading.Event()

    def invoke() -> None:
        with cancel_scope(cancel):
            with pytest.raises(llm_client.MechanismLLMError, match="cancelled"):
                llm_client.chat_completion(settings, system="s", user="u")

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    assert started.wait(timeout=1)
    cancel.set()
    worker.join(timeout=1)
    assert not worker.is_alive()
    release.set()


def test_job_store_claim_stale_uses_lease() -> None:
    """Smoke-test claim SQL shape against a live Postgres when available."""
    import os

    dsn = (os.environ.get("MOLMIND_DATABASE_URL") or "").strip()
    if not dsn:
        pytest.skip("MOLMIND_DATABASE_URL required")
    from agent.memory.jobs_store import BackgroundJobStore, default_lease_owner

    store = BackgroundJobStore(dsn=dsn)
    job_id = f"lease-test-{uuid.uuid4().hex[:10]}"
    owner = default_lease_owner()
    store.upsert(
        {
            "job_id": job_id,
            "kind": "mechanism_pdf",
            "session_id": "s",
            "run_id": "r",
            "status": "running",
            "progress": {},
            "result_ref": {},
            "error": "",
            "cancel_reason": "",
            "payload": {},
            "lease_owner": owner,
            "attempt": 0,
        }
    )
    assert store.acquire_lease(job_id, owner=owner, lease_seconds=2)
    # Fresh lease should not be claimed as stale.
    claimed = store.claim_stale(
        kinds=["mechanism_pdf"],
        stale_seconds=1,
        lease_seconds=30,
        max_attempts=5,
        limit=50,
    )
    assert job_id not in {row.get("job_id") for row in claimed}
    time.sleep(2.2)
    claimed = store.claim_stale(
        kinds=["mechanism_pdf"],
        stale_seconds=1,
        lease_seconds=30,
        max_attempts=5,
        limit=50,
    )
    assert any(row.get("job_id") == job_id for row in claimed)
    row = store.get(job_id)
    assert row is not None
    assert row["status"] == "recovering"
    assert int(row["attempt"]) >= 1
    store.update_status(job_id, status="error", error="test_cleanup", finished=True)


def test_scp_submit_persists_arguments_for_recovery() -> None:
    manager = SCPJobManager(max_workers=1)
    persisted: list[dict] = []

    class FakeStore:
        def upsert(self, job):
            persisted.append(dict(job))

        def get(self, job_id):
            for item in reversed(persisted):
                if item.get("job_id") == job_id:
                    return dict(item)
            return None

        def request_cancel(self, *_a, **_k):
            return True

        def list_by_session(self, *_a, **_k):
            return []

        def claim_stale(self, **_k):
            return []

        def acquire_lease(self, *_a, **_k):
            return True

        def renew_lease(self, *_a, **_k):
            return True

        def release_lease(self, *_a, **_k):
            return True

    manager._store = FakeStore()
    started = threading.Event()
    release = threading.Event()

    def slow():
        started.set()
        release.wait(timeout=2)
        return SimpleNamespace(content=[], status="hit", __dict__={"status": "hit", "content": []})

    submitted = manager.submit(
        slow,
        session_id="session-1",
        skill_id="skill-1",
        tool_id="tool-1",
        run_id="run-1",
        arguments={"q": "fxr"},
        allow_live=True,
    )
    assert started.wait(timeout=1)
    payload = persisted[-1]["payload"]
    assert payload["arguments"] == {"q": "fxr"}
    assert payload["allow_live"] is True
    release.set()
    deadline = time.time() + 2
    while time.time() < deadline:
        job = manager.get(submitted["job_id"], session_id="session-1")
        if job and job.get("status") == "completed":
            break
        time.sleep(0.02)
