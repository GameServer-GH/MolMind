"""机制假说 PDF 异步任务（不阻塞筛选主流程）。状态落 PostgreSQL，字节走 blob。"""

from __future__ import annotations

import base64
import re
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from packages.models import ScoreRecord
from plugins.molmind_core.scientific.evidence_facade.mechanism_graph import MechanismGraph
from plugins.molmind_core.scientific.mechanism import build_mechanism_markdown, markdown_to_pdf_bytes
from plugins.molmind_core.scientific.mechanism.browser_pdf import BrowserPdfUnavailable, html_to_pdf_bytes
from plugins.molmind_core.scientific.mechanism.html_report import build_mechanism_html

_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_CANCEL_EVENTS: dict[str, threading.Event] = {}
_FUTURES: dict[str, Future[Any]] = {}
_LEASE_STOPS: dict[str, threading.Event] = {}
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mech-pdf")
_JOB_STORE = None
_BLOB_STORE = None


def _safe_pdf_filename(source_filename: str) -> str:
    """ASCII-safe download name; keeps Content-Disposition latin-1 compatible."""
    stem = Path(source_filename or "nomination").stem or "nomination"
    safe = re.sub(r"[^\w.\-]+", "_", stem, flags=re.ASCII).strip("._") or "nomination"
    return f"{safe}_mechanism_hypothesis.pdf"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_lease_owner() -> str:
    from agent.memory.jobs_store import default_lease_owner

    return default_lease_owner()


def _start_lease_heartbeat(job_id: str, owner: str) -> None:
    from agent.memory.jobs_store import default_lease_seconds

    stop = threading.Event()
    with _LOCK:
        old = _LEASE_STOPS.pop(job_id, None)
        if old is not None:
            old.set()
        _LEASE_STOPS[job_id] = stop
    lease_seconds = default_lease_seconds()
    interval = max(5.0, lease_seconds / 3.0)

    def _loop() -> None:
        while not stop.wait(interval):
            try:
                if not _job_store().renew_lease(
                    job_id, owner=owner, lease_seconds=lease_seconds
                ):
                    break
            except Exception:  # noqa: BLE001 — heartbeat must not crash the worker
                break

    threading.Thread(
        target=_loop,
        name=f"mech-lease-{job_id[:8]}",
        daemon=True,
    ).start()


def _stop_lease_heartbeat(job_id: str) -> None:
    with _LOCK:
        stop = _LEASE_STOPS.pop(job_id, None)
    if stop is not None:
        stop.set()


def _acquire_runtime_lease(job_id: str, *, status: str = "running") -> str:
    owner = _new_lease_owner()
    try:
        _job_store().acquire_lease(job_id, owner=owner, status=status)
    except Exception:  # noqa: BLE001
        pass
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            job["lease_owner"] = owner
    _start_lease_heartbeat(job_id, owner)
    return owner


def _release_runtime_lease(job_id: str, owner: str = "") -> None:
    _stop_lease_heartbeat(job_id)
    try:
        _job_store().release_lease(job_id, owner=owner)
    except Exception:  # noqa: BLE001
        pass
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            job["lease_owner"] = ""
            job["clear_lease"] = True


def _job_store():
    global _JOB_STORE
    if _JOB_STORE is None:
        from agent.memory import build_job_store

        _JOB_STORE = build_job_store()
    return _JOB_STORE


def _blob_store():
    global _BLOB_STORE
    if _BLOB_STORE is None:
        from agent.memory import build_store

        _BLOB_STORE = build_store().blobs
    return _BLOB_STORE


def _persist(job: dict[str, Any]) -> None:
    payload = {
        "mechanism_pdf_name": job.get("mechanism_pdf_name") or "",
        "pdf_renderer": job.get("pdf_renderer") or "",
        "agent_run_id": job.get("agent_run_id") or "",
        "stage": job.get("stage") or job.get("status") or "pending",
        "resume_inputs": job.get("resume_inputs") or {},
    }
    result_ref = {
        "pdf_blob_id": job.get("pdf_blob_id") or "",
        "md_blob_id": job.get("md_blob_id") or "",
        "html_blob_id": job.get("html_blob_id") or "",
        "mechanism_pdf_name": job.get("mechanism_pdf_name") or "",
        "pdf_renderer": job.get("pdf_renderer") or "",
        "has_md": bool(job.get("mechanism_md")),
        "has_html": bool(job.get("mechanism_html")),
        "stage": job.get("stage") or "",
    }
    if job.get("mechanism_md"):
        payload["mechanism_md"] = job["mechanism_md"]
    if job.get("mechanism_html"):
        payload["mechanism_html"] = job["mechanism_html"]
    _job_store().upsert(
        {
            "job_id": job["job_id"],
            "kind": "mechanism_pdf",
            "session_id": str(job.get("session_id") or ""),
            "run_id": str(job.get("run_id") or job.get("agent_run_id") or ""),
            "status": job.get("status") or "pending",
            "progress": {
                "stage": job.get("stage") or job.get("status") or "pending",
                "stages_done": list(job.get("stages_done") or []),
            },
            "result_ref": result_ref,
            "error": job.get("error") or "",
            "cancel_reason": job.get("cancel_reason") or "",
            "payload": payload,
            "created_at": job.get("created_at") or _now(),
            "updated_at": job.get("updated_at") or _now(),
            "finished_at": (
                job.get("updated_at")
                if job.get("status") in {"ready", "error", "cancelled"}
                else None
            ),
            "lease_owner": str(job.get("lease_owner") or ""),
            "lease_until": job.get("lease_until"),
            "attempt": int(job.get("attempt") or 0),
            "clear_lease": bool(
                job.get("clear_lease")
                or job.get("status") in {"ready", "error", "cancelled"}
            ),
        }
    )


def _hydrate_from_db(job_id: str) -> dict[str, Any] | None:
    row = _job_store().get(job_id)
    if not row or str(row.get("kind") or "") != "mechanism_pdf":
        return None
    payload = row.get("payload") or {}
    result_ref = row.get("result_ref") or {}
    if isinstance(payload, str):
        import json

        payload = json.loads(payload)
    if isinstance(result_ref, str):
        import json

        result_ref = json.loads(result_ref)
    job = {
        "job_id": job_id,
        "status": row.get("status") or "pending",
        "error": row.get("error") or "",
        "mechanism_md": payload.get("mechanism_md") or "",
        "mechanism_html": payload.get("mechanism_html") or "",
        "mechanism_pdf_base64": "",
        "mechanism_pdf_name": (
            payload.get("mechanism_pdf_name")
            or result_ref.get("mechanism_pdf_name")
            or ""
        ),
        "pdf_renderer": payload.get("pdf_renderer") or result_ref.get("pdf_renderer") or "",
        "pdf_blob_id": result_ref.get("pdf_blob_id") or "",
        "md_blob_id": result_ref.get("md_blob_id") or "",
        "html_blob_id": result_ref.get("html_blob_id") or "",
        "stage": (
            (row.get("progress") or {}).get("stage")
            if isinstance(row.get("progress"), dict)
            else payload.get("stage") or row.get("status") or "pending"
        ),
        "stages_done": (
            list((row.get("progress") or {}).get("stages_done") or [])
            if isinstance(row.get("progress"), dict)
            else []
        ),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
        "run_id": row.get("run_id") or "",
        "agent_run_id": payload.get("agent_run_id") or "",
        "cancel_reason": row.get("cancel_reason") or "",
        "session_id": row.get("session_id") or "",
        "resume_inputs": payload.get("resume_inputs") or {},
    }
    if isinstance(row.get("progress"), str):
        import json

        try:
            progress = json.loads(row["progress"])
            job["stage"] = progress.get("stage") or job["stage"]
            job["stages_done"] = list(progress.get("stages_done") or [])
        except json.JSONDecodeError:
            pass
    # Prefer blob-backed markdown/html when payload omitted large text.
    if not job["mechanism_md"] and job.get("md_blob_id"):
        try:
            job["mechanism_md"] = _blob_store().get(str(job["md_blob_id"])).decode("utf-8")
        except Exception:  # noqa: BLE001
            pass
    if not job["mechanism_html"] and job.get("html_blob_id"):
        try:
            job["mechanism_html"] = _blob_store().get(str(job["html_blob_id"])).decode("utf-8")
        except Exception:  # noqa: BLE001
            pass
    blob_id = str(job.get("pdf_blob_id") or "")
    if blob_id and job["status"] == "ready":
        try:
            pdf_bytes = _blob_store().get(blob_id)
            job["mechanism_pdf_base64"] = base64.b64encode(pdf_bytes).decode("ascii")
        except Exception:  # noqa: BLE001
            job["error"] = job.get("error") or "pdf blob missing"
    return job


def get_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job:
            return dict(job)
    return _hydrate_from_db(job_id)


def job_public_view(job: dict[str, Any], *, include_payload: bool = False) -> dict[str, Any]:
    out = {
        "job_id": job["job_id"],
        "status": job["status"],
        "error": job.get("error") or "",
        "mechanism_pdf_name": job.get("mechanism_pdf_name") or "",
        "pdf_renderer": job.get("pdf_renderer") or "",
        "preview_url": f"/api/mechanism/{job['job_id']}/preview",
        "created_at": job.get("created_at") or "",
        "updated_at": job.get("updated_at") or "",
        "cancel_reason": job.get("cancel_reason") or "",
        "stage": job.get("stage") or job.get("status") or "",
        "stages_done": list(job.get("stages_done") or []),
    }
    if include_payload and job.get("status") == "ready":
        out["mechanism_md"] = job.get("mechanism_md") or ""
        out["mechanism_pdf_base64"] = job.get("mechanism_pdf_base64") or ""
        out["mechanism_html"] = job.get("mechanism_html") or ""
    return out


def _update(job_id: str, **fields: Any) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job.update(fields)
        job["updated_at"] = _now()
        stage = str(fields.get("stage") or job.get("stage") or "")
        if stage:
            done = list(job.get("stages_done") or [])
            if stage not in done and stage not in {"pending", "running", "error", "cancelled"}:
                done.append(stage)
                job["stages_done"] = done
        snapshot = dict(job)
    _persist(snapshot)


class _JobCancelled(RuntimeError):
    pass


def _raise_if_cancelled(job_id: str) -> None:
    with _LOCK:
        event = _CANCEL_EVENTS.get(job_id)
        job = _JOBS.get(job_id)
    if event is not None and event.is_set():
        raise _JobCancelled("job cancelled")
    row = _job_store().get(job_id)
    if row and (row.get("cancel_reason") or row.get("status") in {"cancel_requested", "cancelled"}):
        if event is not None:
            event.set()
        raise _JobCancelled("job cancelled")


def cancel_job(job_id: str, *, reason: str = "cancelled") -> bool:
    """Request cancellation and prevent a late PDF payload from being committed."""
    with _LOCK:
        job = _JOBS.get(job_id)
        event = _CANCEL_EVENTS.get(job_id)
        future = _FUTURES.get(job_id)
        if job is None:
            job = _hydrate_from_db(job_id)
            if job is None:
                return False
            _JOBS[job_id] = job
            _CANCEL_EVENTS[job_id] = threading.Event()
            event = _CANCEL_EVENTS[job_id]
        if job.get("status") in {"ready", "error", "cancelled"}:
            return False
        if event is not None:
            event.set()
        cancelled_before_start = bool(future and future.cancel())
        job["status"] = "cancelled" if cancelled_before_start else "cancel_requested"
        job["cancel_reason"] = str(reason or "cancelled")
        job["updated_at"] = _now()
        snapshot = dict(job)
    _persist(snapshot)
    _job_store().request_cancel(job_id, reason=reason)
    return True


def _run_job(
    job_id: str,
    top: list[ScoreRecord],
    llm_cfg: dict[str, Any],
    mark_degraded: Callable[[str], None] | None,
    pdf_name: str,
    assumptions: dict[str, Any],
    run_context: dict[str, str],
    mechanism_graphs: list[MechanismGraph],
) -> None:
    owner = ""
    try:
        _raise_if_cancelled(job_id)
    except _JobCancelled:
        _update(job_id, status="cancelled", stage="cancelled", clear_lease=True)
        return
    owner = _acquire_runtime_lease(job_id, status="running")
    _update(job_id, status="running", stage="markdown", lease_owner=owner)
    try:
        # Resume: reuse persisted markdown/html blobs when present.
        existing = get_job(job_id) or {}
        md = str(existing.get("mechanism_md") or "")
        html = str(existing.get("mechanism_html") or "")
        stages_done = list(existing.get("stages_done") or [])
        if "markdown" not in stages_done or not md:
            md = build_mechanism_markdown(
                top,
                llm_cfg=llm_cfg,
                mark_degraded=mark_degraded,
                assumptions=assumptions,
                run_context=run_context,
                mechanism_graphs=mechanism_graphs,
            )
            md_blob = _blob_store().put(
                md.encode("utf-8"),
                kind="mechanism_md",
                media_type="text/markdown",
                session_id=str(run_context.get("session_id") or ""),
            )
            _update(
                job_id,
                stage="markdown",
                mechanism_md=md,
                md_blob_id=md_blob["blob_id"],
            )
        _raise_if_cancelled(job_id)
        _update(job_id, stage="html")
        if "html" not in stages_done or not html:
            html = build_mechanism_html(
                top,
                assumptions=assumptions,
                run_context=run_context,
            )
            html_blob = _blob_store().put(
                html.encode("utf-8"),
                kind="mechanism_html",
                media_type="text/html",
                session_id=str(run_context.get("session_id") or ""),
            )
            _update(
                job_id,
                stage="html",
                mechanism_html=html,
                html_blob_id=html_blob["blob_id"],
            )
        _raise_if_cancelled(job_id)
        _update(job_id, stage="pdf")
        renderer = "html_chromium"
        try:
            pdf_bytes = html_to_pdf_bytes(html)
        except BrowserPdfUnavailable:
            renderer = "reportlab_fallback"
            if mark_degraded is not None:
                mark_degraded("html_pdf_renderer_unavailable")
            pdf_bytes = markdown_to_pdf_bytes(md)
        _raise_if_cancelled(job_id)
        blob = _blob_store().put(
            pdf_bytes,
            kind="mechanism_pdf",
            media_type="application/pdf",
            session_id=str(run_context.get("session_id") or ""),
        )
        _update(
            job_id,
            status="ready",
            stage="ready",
            mechanism_md=md,
            mechanism_html=html,
            mechanism_pdf_base64=base64.b64encode(pdf_bytes).decode("ascii"),
            mechanism_pdf_name=pdf_name,
            pdf_renderer=renderer,
            pdf_blob_id=blob["blob_id"],
            error="",
            clear_lease=True,
        )
    except _JobCancelled:
        _update(
            job_id,
            status="cancelled",
            stage="cancelled",
            mechanism_md="",
            mechanism_html="",
            mechanism_pdf_base64="",
            clear_lease=True,
        )
    except Exception as exc:  # noqa: BLE001 — 后台任务边界
        _update(job_id, status="error", stage="error", error=str(exc), clear_lease=True)
    finally:
        _release_runtime_lease(job_id, owner)
        with _LOCK:
            _FUTURES.pop(job_id, None)


def _build_resume_inputs(
    top: list[ScoreRecord],
    *,
    llm_cfg: dict[str, Any] | None,
    assumptions: dict[str, Any] | None,
    run_context: dict[str, str] | None,
    mechanism_graphs: list[MechanismGraph] | None,
    pdf_name: str,
) -> dict[str, Any]:
    from packages.models import serialize_record

    serialized_top: list[dict[str, Any]] = []
    for item in top:
        try:
            serialized_top.append(serialize_record(item))
        except Exception:  # noqa: BLE001 — keep job startable in unit stubs
            continue
    serialized_graphs: list[dict[str, Any]] = []
    for graph in mechanism_graphs or []:
        try:
            serialized_graphs.append(graph.to_dict())
        except Exception:  # noqa: BLE001
            continue
    return {
        "top": serialized_top,
        "llm_cfg": dict(llm_cfg or {}),
        "assumptions": dict(assumptions or {}),
        "run_context": dict(run_context or {}),
        "mechanism_graphs": serialized_graphs,
        "pdf_name": pdf_name,
    }


def _rehydrate_score_record(data: dict[str, Any]) -> ScoreRecord:
    from dataclasses import fields

    known = {item.name for item in fields(ScoreRecord)}
    cleaned = {key: value for key, value in dict(data or {}).items() if key in known}
    cleaned.pop("fp_bits", None)
    cleaned["attributions"] = []
    cleaned["evidence_hits"] = []
    for key in ("eligibility_reasons", "audit_missing"):
        if isinstance(cleaned.get(key), list):
            cleaned[key] = tuple(cleaned[key])
    return ScoreRecord(**cleaned)


def _rehydrate_mechanism_graph(data: dict[str, Any]) -> MechanismGraph:
    from plugins.molmind_core.scientific.evidence_facade.mechanism_graph import MechanismEdge

    edges = tuple(
        MechanismEdge(
            source=str(edge.get("source") or ""),
            target=str(edge.get("target") or ""),
            relation=str(edge.get("relation") or ""),
            evidence_level=str(edge.get("evidence_level") or ""),
            directness=str(edge.get("directness") or ""),
            evidence_role=str(edge.get("evidence_role") or ""),
            evidence_ids=tuple(edge.get("evidence_ids") or ()),
            notes=tuple(edge.get("notes") or ()),
        )
        for edge in (data.get("edges") or [])
        if isinstance(edge, dict)
    )
    return MechanismGraph(
        molecule_id=str(data.get("molecule_id") or ""),
        inchikey=str(data.get("inchikey") or ""),
        target_symbol=data.get("target_symbol"),
        chain_status=str(data.get("chain_status") or ""),
        context_snapshot_sha256=str(data.get("context_snapshot_sha256") or ""),
        edges=edges,
        evidence_gaps=tuple(data.get("evidence_gaps") or ()),
    )


def _dispatch_job(
    job_id: str,
    top: list[ScoreRecord],
    llm_cfg: dict[str, Any],
    mark_degraded: Callable[[str], None] | None,
    pdf_name: str,
    assumptions: dict[str, Any],
    run_context: dict[str, str],
    mechanism_graphs: list[MechanismGraph],
) -> None:
    future = _EXECUTOR.submit(
        _run_job,
        job_id,
        list(top),
        dict(llm_cfg or {}),
        mark_degraded,
        pdf_name,
        dict(assumptions or {}),
        run_context,
        list(mechanism_graphs or []),
    )
    with _LOCK:
        _FUTURES[job_id] = future
    future.add_done_callback(lambda _future: _forget_future(job_id))


def resume_mechanism_job(job_id: str) -> bool:
    """Re-dispatch an orphaned job, skipping stages already recorded in progress."""
    job = _hydrate_from_db(job_id)
    if job is None:
        return False
    if str(job.get("status") or "") in {"ready", "error", "cancelled"}:
        return False
    if str(job.get("cancel_reason") or "") or str(job.get("status") or "") in {
        "cancel_requested",
        "cancelled",
    }:
        return False
    with _LOCK:
        _JOBS[job_id] = job
        _CANCEL_EVENTS.setdefault(job_id, threading.Event())
    stages_done = list(job.get("stages_done") or [])
    has_md = bool(job.get("mechanism_md"))
    has_html = bool(job.get("mechanism_html"))
    resume = dict(job.get("resume_inputs") or {})
    # PDF-only resume: markdown+html already materialised.
    pdf_only = (
        "markdown" in stages_done
        and has_md
        and "html" in stages_done
        and has_html
    )
    if pdf_only:
        top: list[ScoreRecord] = []
        graphs: list[MechanismGraph] = []
        llm_cfg = dict(resume.get("llm_cfg") or {})
        assumptions = dict(resume.get("assumptions") or {})
        run_context = dict(resume.get("run_context") or {})
        pdf_name = str(resume.get("pdf_name") or job.get("mechanism_pdf_name") or "mechanism.pdf")
    else:
        top_payload = resume.get("top") or []
        if not isinstance(top_payload, list) or not top_payload:
            _update(
                job_id,
                status="error",
                stage="error",
                error="orphaned_missing_resume_inputs",
            )
            return False
        try:
            top = [_rehydrate_score_record(item) for item in top_payload if isinstance(item, dict)]
            graphs = [
                _rehydrate_mechanism_graph(item)
                for item in (resume.get("mechanism_graphs") or [])
                if isinstance(item, dict)
            ]
        except Exception as exc:  # noqa: BLE001
            _update(
                job_id,
                status="error",
                stage="error",
                error=f"orphaned_resume_rehydrate_failed:{exc}",
            )
            return False
        llm_cfg = dict(resume.get("llm_cfg") or {})
        assumptions = dict(resume.get("assumptions") or {})
        run_context = dict(resume.get("run_context") or {})
        pdf_name = str(resume.get("pdf_name") or job.get("mechanism_pdf_name") or "mechanism.pdf")

    _update(job_id, status="pending", stage=job.get("stage") or "pending", error="")
    _dispatch_job(
        job_id,
        top,
        llm_cfg,
        None,
        pdf_name,
        assumptions,
        {str(k): str(v) for k, v in run_context.items()},
        graphs,
    )
    return True


def recover_orphan_jobs(*, stale_seconds: int = 120, limit: int = 50) -> list[str]:
    """Claim stale mechanism jobs and re-dispatch them (skip completed stages)."""
    recovered: list[str] = []
    try:
        rows = _job_store().claim_stale(
            kinds=["mechanism_pdf"],
            stale_seconds=stale_seconds,
            limit=limit,
        )
    except Exception:  # noqa: BLE001 — recovery must not block API startup
        return recovered
    for row in rows:
        job_id = str(row.get("job_id") or "")
        if not job_id:
            continue
        if resume_mechanism_job(job_id):
            recovered.append(job_id)
    return recovered


def start_mechanism_job(
    top: list[ScoreRecord],
    *,
    llm_cfg: dict[str, Any] | None,
    mark_degraded: Callable[[str], None] | None,
    source_filename: str = "",
    assumptions: dict[str, Any] | None = None,
    run_context: dict[str, str] | None = None,
    mechanism_graphs: list[MechanismGraph] | None = None,
) -> str:
    """提交异步机制生成；立即返回 job_id。空候选则直接 ready 空结果。"""
    job_id = uuid.uuid4().hex
    pdf_name = _safe_pdf_filename(source_filename)
    now = _now()
    ctx = dict(run_context or {})
    resume_inputs = _build_resume_inputs(
        top,
        llm_cfg=llm_cfg,
        assumptions=assumptions,
        run_context=ctx,
        mechanism_graphs=mechanism_graphs,
        pdf_name=pdf_name,
    )
    with _LOCK:
        _JOBS[job_id] = {
            "job_id": job_id,
            "status": "pending",
            "stage": "pending",
            "stages_done": [],
            "error": "",
            "mechanism_md": "",
            "mechanism_html": "",
            "mechanism_pdf_base64": "",
            "mechanism_pdf_name": pdf_name,
            "pdf_renderer": "",
            "pdf_blob_id": "",
            "md_blob_id": "",
            "html_blob_id": "",
            "created_at": now,
            "updated_at": now,
            "run_id": str(ctx.get("run_id") or ""),
            "agent_run_id": str(ctx.get("agent_run_id") or ""),
            "session_id": str(ctx.get("session_id") or ""),
            "cancel_reason": "",
            "resume_inputs": resume_inputs,
        }
        _CANCEL_EVENTS[job_id] = threading.Event()
        snapshot = dict(_JOBS[job_id])
    _persist(snapshot)

    if not top:
        _update(job_id, status="ready")
        return job_id

    _dispatch_job(
        job_id,
        list(top),
        dict(llm_cfg or {}),
        mark_degraded,
        pdf_name,
        dict(assumptions or {}),
        ctx,
        list(mechanism_graphs or []),
    )
    return job_id


def _forget_future(job_id: str) -> None:
    with _LOCK:
        _FUTURES.pop(job_id, None)
