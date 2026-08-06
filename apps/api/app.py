"""MolMind FastAPI：上传 SDF → 预览 Top N → CSV；机制 PDF 异步生成。

API surface lineage: mm-LJR
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from apps.api.agent_routes import (
    router as agent_router,
    start_run_queue_workers,
    stop_run_queue_workers,
)
from apps.api.download_headers import content_disposition_attachment
from plugins.molmind_core.scientific.mechanism.jobs import cancel_job, get_job, job_public_view, start_mechanism_job
from plugins.molmind_core.scientific.nomination import (
    apply_selected_proposals,
    build_interactive_review_proposals,
    get_review_session,
    payload_from_applied,
    store_review_session,
)
from plugins.molmind_core.scientific.pipeline import load_config, screen_sdf
from plugins.molmind_core.scientific.pipeline.config_loader import resolve_runtime_switches
from plugins.molmind_core.scientific.pipeline.export import reserve_shortage_note
from plugins.molmind_core.scientific.pipeline.run_log import RunLogEntry
from plugins.molmind_core.scientific.pipeline.runner import TOP_N_MAX, TOP_N_MIN

STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
_LEGACY_MODES = frozenset({"auto", "online", "offline"})
_EXECUTOR = ThreadPoolExecutor(max_workers=2)
# build watermark — LJR
_API_BUILD_MARK = "mm.ljr.api"

def _resolve_app_version() -> str:
    """Use the checked-out project version before stale installed metadata."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
        match = re.search(r"(?m)^\s*version\s*=\s*[\"']([^\"']+)[\"']", text)
        if match:
            return match.group(1)
    except OSError:
        pass
    try:
        return package_version("molmind")
    except PackageNotFoundError:
        return "unknown"


APP_VERSION = _resolve_app_version()

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    start_run_queue_workers()
    try:
        yield
    finally:
        stop_run_queue_workers()


app = FastAPI(title="MolMind", version=APP_VERSION, lifespan=_lifespan)
app.include_router(agent_router)
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

def _clamp_top(top: int) -> int:
    if top < TOP_N_MIN or top > TOP_N_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"top 须在 {TOP_N_MIN}–{TOP_N_MAX} 之间",
        )
    return top


def _screen_config(
    *,
    mode: str = "auto",
    use_snapshot: bool | None = True,
    allow_live: bool | None = None,
    epa_stage: Optional[int] = None,
):
    """Load Quality-Max config; legacy ``mode`` only seeds switches when omitted."""
    requested = (mode or "auto").lower().strip()
    if requested == "quality-max":
        requested = "auto"
    if requested not in _LEGACY_MODES:
        raise HTTPException(
            status_code=400,
            detail="mode 已弃用，请使用 allow_live / use_snapshot；兼容值：auto|online|offline",
        )
    try:
        return load_config(
            mode=requested,
            use_snapshot=use_snapshot,
            allow_live=allow_live,
            epa_stage=epa_stage,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _start_mechanism_async(result: Any, *, log_sink: Any | None = None) -> str:
    """启动机制 PDF 后台任务，立即返回 job_id（不阻塞筛选结果）。"""
    return _start_mechanism_job_for_top(
        result.top_molecules,
        llm_cfg=result.config.llm,
        source_filename=result.source_filename or "",
        assumptions=result.config.assumptions,
        run_context={
            "run_id": result.run_id,
            "input_sha256": result.input_sha256,
            "config_hash": result.config.config_hash,
            "selection_sha256": result.selection_sha256,
        },
        mechanism_graphs=result.mechanism_graphs,
        mark_degraded=result.config.mark_degraded,
        log_sink=log_sink,
    )


def _start_mechanism_job_for_top(
    top: list[Any],
    *,
    llm_cfg: dict[str, Any] | None,
    source_filename: str,
    assumptions: dict[str, Any] | None,
    run_context: dict[str, str],
    mechanism_graphs: list[Any] | None = None,
    mark_degraded: Any | None = None,
    log_sink: Any | None = None,
) -> str:
    if log_sink is not None:
        log_sink(
            RunLogEntry(
                level="INFO",
                message="筛选完成；机制与验证方案 PDF 已在后台异步生成（不阻塞主流程）",
                lang="zh",
                progress=100,
            )
        )
        log_sink(
            RunLogEntry(
                level="INFO",
                message="Screening done; mechanism PDF generating asynchronously (non-blocking)",
                lang="en",
                progress=None,
            )
        )
    return start_mechanism_job(
        top,
        llm_cfg=llm_cfg,
        mark_degraded=mark_degraded,
        source_filename=source_filename or "",
        assumptions=assumptions,
        run_context=run_context,
        mechanism_graphs=mechanism_graphs,
    )


def _result_payload(
    result: Any,
    *,
    mechanism_job_id: str = "",
    nomination_review: bool = False,
) -> dict:
    evidence = result.config.raw.get("evidence") or {}
    use_snapshot = bool(evidence.get("use_snapshot", True))
    allow_live = bool(evidence.get("allow_live", False))
    epa_cfg = evidence.get("epa_ctx") or {}
    review_pending = bool(nomination_review)
    summary = {
        "source": result.source_filename,
        "raw_count": result.raw_count,
        "parse_skipped": result.parse_skipped,
        "inchikey_missing": result.inchikey_missing,
        "input_count": result.input_count,
        "filtered_out": result.filtered_out,
        "review_required_count": result.review_required_count,
        "eligible_count": result.eligible_count,
        "output_count": result.output_count,
        "reserve_count": len(result.reserve_molecules),
        "requested_top_n": result.requested_top_n,
        "mode": "auto",
        "quality_max": True,
        "use_snapshot": use_snapshot,
        "allow_live": allow_live,
        "epa_stage": int(epa_cfg.get("integration_stage", 0)),
        "config_hash": result.config.config_hash,
        "run_id": result.run_id,
        "input_sha256": result.input_sha256,
        "selection_sha256": result.selection_sha256,
        "reserve_selection_sha256": result.reserve_selection_sha256,
        "primary_label": f"候选分子清单：Top {result.output_count}",
        "reserve_label": (
            "候补名单：仅在主榜候选不可采购、无法配制或身份复核失败时"
            "按冻结顺序顺延，不参与主榜并列排名"
        ),
        "reserve_note": (
            reserve_shortage_note(
                actual_count=len(result.reserve_molecules),
                requested_count=result.config.reserve_n,
            )
            if len(result.reserve_molecules) < result.config.reserve_n
            else ""
        ),
        "degraded_channels": result.config.degraded_channels,
        "diagnostics": {
            "std_tox": result.diagnostics.std_tox,
            "scaffold_diversity_top10": result.diagnostics.scaffold_diversity_top10,
            "quality_pass": result.diagnostics.quality_pass,
            "engineering_pass": result.diagnostics.engineering_pass,
            "model_coverage_status": result.diagnostics.model_coverage_status,
            "scientific_validation_status": result.diagnostics.scientific_validation_status,
            "evidence_coverage_ratio": result.diagnostics.evidence_coverage_ratio,
            "parse_skipped": result.parse_skipped,
        },
        "note": result.note,
        "mechanism_job_id": mechanism_job_id,
        "nomination_review": bool(nomination_review),
        "review_pending": review_pending,
    }
    payload = {
        "summary": summary,
        "rows": result.to_row_dicts(),
        "reserve_rows": result.to_reserve_row_dicts(),
        "csv": result.to_csv_text(),
        "reserve_csv": result.to_reserve_csv_text(),
        "logs": result.logs,
        "mechanism_graphs": [graph.to_dict() for graph in result.mechanism_graphs],
        "hepg2_ffa_resources": result.hepg2_ffa_resources,
        "mechanism_job_id": mechanism_job_id,
        # 兼容旧前端字段：异步后初值为空
        "mechanism_md": "",
        "mechanism_pdf_base64": "",
        "mechanism_pdf_name": "",
    }
    if nomination_review:
        # Web 交互复核：算法榜就绪后门控；确认前不启动机制 PDF。
        bundle = build_interactive_review_proposals(
            result.top_molecules,
            result.reserve_molecules,
            use_llm=True,
            llm_cfg=dict(result.config.llm or {}),
        )
        review_dict = bundle.to_dict()
        if review_dict.get("note"):
            review_dict["note"] = (
                f"{review_dict['note']}；确认后将生成最终 CSV 与机制假说 PDF"
            )
        else:
            review_dict["note"] = (
                "算法主榜已就绪，等待人工确认；确认后将生成最终 CSV 与机制假说 PDF"
            )
        store_review_session(
            result.run_id,
            top=result.top_molecules,
            reserve=result.reserve_molecules,
            proposals=bundle.proposals,
            mode=result.config.mode,
            config_hash=result.config.config_hash,
            input_sha256=result.input_sha256,
            degraded_channels=result.config.degraded_channels,
            summary=summary,
            source_filename=result.source_filename or "",
            llm_cfg=dict(result.config.llm or {}),
            assumptions=dict(result.config.assumptions or {}),
            hepg2_ffa_resources=dict(result.hepg2_ffa_resources or {}),
            logs=list(result.logs or []),
        )
        payload["interactive_review"] = review_dict
    else:
        payload["interactive_review"] = {
            "enabled": False,
            "requires_human_confirm": False,
            "llm_used": False,
            "draft_engine": "none",
            "note": "未开启 LLM+人工复核；筛选结束后直接出结果（Web 可勾选「LLM+人工复核」）",
            "proposals": [],
            "applied": False,
        }
    return payload


class ApplyReviewRequest(BaseModel):
    run_id: str
    selected_proposal_ids: list[str] = Field(default_factory=list)
    mechanism_job_id: str = ""


@app.get("/")
async def index() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.is_file():
        raise HTTPException(status_code=404, detail="web index missing")
    return FileResponse(index_path, media_type="text/html; charset=utf-8")


@app.get("/favicon.ico")
async def favicon() -> Response:
    icon = STATIC_DIR / "favicon.png"
    if icon.is_file():
        return FileResponse(icon, media_type="image/png")
    return Response(status_code=204)


@app.get("/health")
def health() -> dict:
    cfg = load_config()
    _, allow_live, use_snapshot = resolve_runtime_switches()
    return {
        "status": "ok",
        "version": APP_VERSION,
        "build": _API_BUILD_MARK,
        "config_hash": cfg.config_hash,
        "mode": "auto",
        "quality_max": True,
        "use_snapshot": use_snapshot,
        "allow_live": allow_live,
        "top_n_min": TOP_N_MIN,
        "top_n_max": TOP_N_MAX,
    }


@app.get("/api/mechanism/{job_id}")
def mechanism_status(job_id: str, include_payload: bool = False) -> dict:
    """查询异步机制 PDF 状态；ready 时可带 payload。"""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="mechanism job 不存在或已过期")
    return job_public_view(job, include_payload=include_payload)


@app.delete("/api/mechanism/{job_id}")
def mechanism_cancel(job_id: str) -> dict:
    if not get_job(job_id):
        raise HTTPException(status_code=404, detail="mechanism job 不存在或已过期")
    if not cancel_job(job_id, reason="user_cancelled"):
        raise HTTPException(status_code=409, detail="机制任务已结束")
    return {"job_id": job_id, "status": "cancel_requested"}


@app.get("/api/mechanism/{job_id}/download")
def mechanism_download(job_id: str) -> Response:
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="mechanism job 不存在或已过期")
    if job.get("status") != "ready":
        raise HTTPException(status_code=409, detail=f"机制 PDF 尚未就绪（status={job.get('status')}）")
    b64 = job.get("mechanism_pdf_base64") or ""
    if not b64:
        raise HTTPException(status_code=404, detail="机制 PDF 为空")
    name = job.get("mechanism_pdf_name") or "mechanism_hypothesis.pdf"
    return Response(
        content=base64.b64decode(b64),
        media_type="application/pdf",
        headers={"Content-Disposition": content_disposition_attachment(name)},
    )


@app.get("/api/mechanism/{job_id}/preview", response_class=HTMLResponse)
def mechanism_preview(job_id: str) -> HTMLResponse:
    """Preview the exact HTML used by Chromium for PDF generation."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="机制报告不存在或已过期")
    if job.get("status") != "ready":
        raise HTTPException(status_code=409, detail=f"机制报告尚未就绪（status={job.get('status')}）")
    html = str(job.get("mechanism_html") or "")
    if not html:
        raise HTTPException(status_code=404, detail="机制 HTML 预览为空")
    return HTMLResponse(content=html)


@app.post("/api/screen")
async def screen_endpoint(
    file: UploadFile = File(...),
    top: int = 10,
    mode: str = "auto",
    use_snapshot: bool = True,
    allow_live: Optional[bool] = None,
    epa_stage: Optional[int] = None,
    nomination_review: bool = False,
) -> dict:
    top = _clamp_top(top)

    if not file.filename or not file.filename.lower().endswith(".sdf"):
        raise HTTPException(status_code=400, detail="请上传 .sdf 文件")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件为空")

    with tempfile.NamedTemporaryFile(suffix=".sdf", delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        cfg = _screen_config(
            mode=mode,
            use_snapshot=use_snapshot,
            allow_live=allow_live,
            epa_stage=epa_stage,
        )
        result = screen_sdf(tmp_path, cfg=cfg, top_n=top, source_filename=file.filename)
        job_id = ""
        if not nomination_review:
            job_id = _start_mechanism_async(result)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    return _result_payload(
        result,
        mechanism_job_id=job_id,
        nomination_review=nomination_review,
    )


@app.post("/api/screen/stream")
async def screen_stream(
    file: UploadFile = File(...),
    top: int = 10,
    mode: str = "auto",
    use_snapshot: bool = True,
    allow_live: Optional[bool] = None,
    epa_stage: Optional[int] = None,
    nomination_review: bool = False,
) -> StreamingResponse:
    """NDJSON 流：筛选日志 + result（含 mechanism_job_id）；PDF 后台生成。"""
    top = _clamp_top(top)
    review_flag = bool(nomination_review)

    if not file.filename or not file.filename.lower().endswith(".sdf"):
        raise HTTPException(status_code=400, detail="请上传 .sdf 文件")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件为空")

    filename = file.filename
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    def on_log(entry) -> None:
        loop.call_soon_threadsafe(
            queue.put_nowait,
            {"type": "log", **entry.to_dict()},
        )

    def run_job() -> None:
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".sdf", delete=False) as tmp:
                tmp.write(content)
                tmp_path = Path(tmp.name)
            cfg = _screen_config(
                mode=mode,
                use_snapshot=use_snapshot,
                allow_live=allow_live,
                epa_stage=epa_stage,
            )
            result = screen_sdf(
                tmp_path,
                cfg=cfg,
                top_n=top,
                source_filename=filename,
                log_sink=on_log,
            )
            if review_flag:
                on_log(
                    RunLogEntry(
                        level="INFO",
                        message="算法主榜已就绪，等待人工复核确认后再生成机制 PDF",
                        lang="zh",
                        progress=92,
                    )
                )
                on_log(
                    RunLogEntry(
                        level="INFO",
                        message="Algorithmic shortlist ready; awaiting interactive review before mechanism PDF",
                        lang="en",
                        progress=None,
                    )
                )
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {
                        "type": "review_pending",
                        **_result_payload(
                            result,
                            mechanism_job_id="",
                            nomination_review=True,
                        ),
                    },
                )
            else:
                job_id = _start_mechanism_async(result, log_sink=on_log)
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {
                        "type": "result",
                        **_result_payload(
                            result,
                            mechanism_job_id=job_id,
                            nomination_review=False,
                        ),
                    },
                )
        except Exception as exc:  # noqa: BLE001
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"type": "error", "detail": str(exc)},
            )
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            loop.call_soon_threadsafe(queue.put_nowait, None)

    async def generate():
        _EXECUTOR.submit(run_job)
        while True:
            item = await queue.get()
            if item is None:
                break
            yield json.dumps(item, ensure_ascii=False) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@app.post("/api/screen/apply-review")
def screen_apply_review(body: ApplyReviewRequest) -> dict:
    """Apply human-selected interactive review proposals and finalize deliverables."""
    from plugins.molmind_core.scientific.evidence_facade.mechanism_graph import (
        build_mechanism_graphs,
        load_mechanism_context,
    )
    from plugins.molmind_core.scientific.pipeline.run_identity import selection_sha256 as sel_hash

    session = get_review_session(body.run_id)
    if not session:
        raise HTTPException(
            status_code=404,
            detail=(
                f"复核会话不存在或已过期（run_id={body.run_id}）；"
                "请用 nomination_review=true 重新筛选"
            ),
        )
    applied = apply_selected_proposals(
        top=session["top"],
        reserve=session["reserve"],
        proposals=session.get("proposals") or [],
        selected_proposal_ids=body.selected_proposal_ids,
        top_n=len(session["top"]),
        reserve_n=len(session["reserve"]),
    )
    review_payload = {
        "enabled": True,
        "requires_human_confirm": True,
        "llm_used": False,
        "draft_engine": "rules",
        "note": "人工已确认所选提案；已启动最终机制 PDF",
        "proposals": session.get("proposals") or [],
        "applied": True,
        "applied_proposal_ids": applied.applied_proposal_ids,
        "actions": applied.actions,
    }
    # Refresh session so subsequent applies start from the confirmed board.
    store_review_session(
        body.run_id,
        top=applied.top,
        reserve=applied.reserve,
        proposals=session.get("proposals") or [],
        mode=session["mode"],
        config_hash=session["config_hash"],
        input_sha256=session["input_sha256"],
        degraded_channels=session.get("degraded_channels") or [],
        summary=session.get("summary") or {},
        source_filename=str(session.get("source_filename") or ""),
        llm_cfg=session.get("llm_cfg") or {},
        assumptions=session.get("assumptions") or {},
        hepg2_ffa_resources=session.get("hepg2_ffa_resources") or {},
        logs=session.get("logs") or [],
    )
    mech_ctx, mech_sha = load_mechanism_context()
    mechanism_graphs = build_mechanism_graphs(
        applied.top,
        context=mech_ctx,
        context_sha256=mech_sha,
    )
    selection = sel_hash(applied.top)
    job_id = _start_mechanism_job_for_top(
        applied.top,
        llm_cfg=session.get("llm_cfg") or {},
        source_filename=str(session.get("source_filename") or ""),
        assumptions=session.get("assumptions") or {},
        run_context={
            "run_id": body.run_id,
            "input_sha256": str(session.get("input_sha256") or ""),
            "config_hash": str(session.get("config_hash") or ""),
            "selection_sha256": selection,
        },
        mechanism_graphs=mechanism_graphs,
        mark_degraded=None,
    )
    return payload_from_applied(
        run_id=body.run_id,
        top=applied.top,
        reserve=applied.reserve,
        mode=session["mode"],
        config_hash=session["config_hash"],
        input_sha256=session["input_sha256"],
        degraded_channels=session.get("degraded_channels") or [],
        base_summary=session.get("summary") or {},
        interactive_review=review_payload,
        mechanism_job_id=job_id,
        logs=session.get("logs") or [],
        mechanism_graphs=[g.to_dict() for g in mechanism_graphs],
        hepg2_ffa_resources=session.get("hepg2_ffa_resources") or {},
    )


@app.post("/api/screen/download")
async def screen_download(
    file: UploadFile = File(...),
    top: int = 10,
    mode: str = "auto",
    use_snapshot: bool = True,
    allow_live: Optional[bool] = None,
    epa_stage: Optional[int] = None,
    nomination_review: bool = False,
    tier: str = "primary",
) -> Response:
    export_tier = (tier or "primary").strip().lower()
    if export_tier not in {"primary", "reserve"}:
        raise HTTPException(status_code=400, detail="tier 须为 primary 或 reserve")
    payload = await screen_endpoint(
        file=file,
        top=top,
        mode=mode,
        use_snapshot=use_snapshot,
        allow_live=allow_live,
        epa_stage=epa_stage,
        nomination_review=nomination_review,
    )
    source = Path(file.filename or "library.sdf").stem or "library"
    if export_tier == "reserve":
        content = payload["reserve_csv"]
        filename = f"{source}_nomination_reserve.csv"
    else:
        content = payload["csv"]
        filename = f"{source}_nomination_top10.csv"
    return Response(
        content="\ufeff" + content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": content_disposition_attachment(filename)},
    )
