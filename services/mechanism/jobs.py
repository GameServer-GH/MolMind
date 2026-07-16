"""机制假说 PDF 异步任务（不阻塞筛选主流程）。"""

from __future__ import annotations

import base64
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from packages.models import ScoreRecord
from services.evidence_facade.mechanism_graph import MechanismGraph
from services.mechanism import build_mechanism_markdown, markdown_to_pdf_bytes
from services.mechanism.browser_pdf import BrowserPdfUnavailable, html_to_pdf_bytes
from services.mechanism.html_report import build_mechanism_html

_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mech-pdf")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


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
    _update(job_id, status="running")
    try:
        md = build_mechanism_markdown(
            top,
            llm_cfg=llm_cfg,
            mark_degraded=mark_degraded,
            assumptions=assumptions,
            run_context=run_context,
            mechanism_graphs=mechanism_graphs,
        )
        html = build_mechanism_html(
            top,
            assumptions=assumptions,
            run_context=run_context,
        )
        renderer = "html_chromium"
        try:
            pdf_bytes = html_to_pdf_bytes(html)
        except BrowserPdfUnavailable:
            renderer = "reportlab_fallback"
            if mark_degraded is not None:
                mark_degraded("html_pdf_renderer_unavailable")
            pdf_bytes = markdown_to_pdf_bytes(md)
        _update(
            job_id,
            status="ready",
            mechanism_md=md,
            mechanism_html=html,
            mechanism_pdf_base64=base64.b64encode(pdf_bytes).decode("ascii"),
            mechanism_pdf_name=pdf_name,
            pdf_renderer=renderer,
            error="",
        )
    except Exception as exc:  # noqa: BLE001 — 后台任务边界
        _update(job_id, status="error", error=str(exc))


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
    stem = (source_filename or "nomination").replace(".sdf", "").replace(".SDF", "")
    pdf_name = f"{stem}_mechanism_hypothesis.pdf"
    now = _now()
    with _LOCK:
        _JOBS[job_id] = {
            "job_id": job_id,
            "status": "pending",
            "error": "",
            "mechanism_md": "",
            "mechanism_html": "",
            "mechanism_pdf_base64": "",
            "mechanism_pdf_name": pdf_name,
            "pdf_renderer": "",
            "created_at": now,
            "updated_at": now,
        }

    if not top:
        _update(job_id, status="ready")
        return job_id

    # 拷贝列表引用即可（同进程）；打分结果已冻结
    _EXECUTOR.submit(
        _run_job,
        job_id,
        list(top),
        dict(llm_cfg or {}),
        mark_degraded,
        pdf_name,
        dict(assumptions or {}),
        dict(run_context or {}),
        list(mechanism_graphs or []),
    )
    return job_id
