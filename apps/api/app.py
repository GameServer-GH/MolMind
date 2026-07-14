"""MolMind FastAPI：上传 SDF → 预览 Top N → CSV；机制 PDF 异步生成。

API surface lineage: mm-LJR
"""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from services.mechanism.jobs import get_job, job_public_view, start_mechanism_job
from services.pipeline import load_config, screen_sdf
from services.pipeline.run_log import RunLogEntry
from services.pipeline.runner import TOP_N_MAX, TOP_N_MIN

STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
ALLOWED_MODES = frozenset({"auto", "online", "offline"})
_EXECUTOR = ThreadPoolExecutor(max_workers=2)
# build watermark — LJR
_API_BUILD_MARK = "mm.ljr.api"

app = FastAPI(title="MolMind", version="0.1.0")
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _clamp_top(top: int) -> int:
    if top < TOP_N_MIN or top > TOP_N_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"top 须在 {TOP_N_MIN}–{TOP_N_MAX} 之间",
        )
    return top


def _resolve_mode(mode: str) -> str:
    resolved = (mode or "auto").lower().strip()
    if resolved == "quality-max":
        resolved = "auto"
    if resolved not in ALLOWED_MODES:
        raise HTTPException(
            status_code=400,
            detail="mode 须为 auto（Quality-Max）| online | offline",
        )
    return resolved


def _apply_snapshot_flag(cfg: Any, use_snapshot: bool) -> None:
    evidence = cfg.raw.setdefault("evidence", {})
    evidence["use_snapshot"] = bool(use_snapshot)
    if not use_snapshot:
        evidence["prefer_snapshot"] = False
    elif "prefer_snapshot" not in evidence:
        evidence["prefer_snapshot"] = True


def _start_mechanism_async(result: Any, *, log_sink: Any | None = None) -> str:
    """启动机制 PDF 后台任务，立即返回 job_id（不阻塞筛选结果）。"""
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
        result.top_molecules,
        llm_cfg=result.config.llm,
        mark_degraded=result.config.mark_degraded,
        source_filename=result.source_filename or "",
    )


def _result_payload(result: Any, *, mechanism_job_id: str = "") -> dict:
    evidence = result.config.raw.get("evidence") or {}
    use_snapshot = bool(evidence.get("use_snapshot", True))
    return {
        "summary": {
            "source": result.source_filename,
            "raw_count": result.raw_count,
            "parse_skipped": result.parse_skipped,
            "inchikey_missing": result.inchikey_missing,
            "input_count": result.input_count,
            "filtered_out": result.filtered_out,
            "eligible_count": result.eligible_count,
            "output_count": result.output_count,
            "requested_top_n": result.requested_top_n,
            "mode": result.config.mode,
            "use_snapshot": use_snapshot,
            "config_hash": result.config.config_hash,
            "degraded_channels": result.config.degraded_channels,
            "diagnostics": {
                "std_tox": result.diagnostics.std_tox,
                "scaffold_diversity_top10": result.diagnostics.scaffold_diversity_top10,
                "quality_pass": result.diagnostics.quality_pass,
                "parse_skipped": result.parse_skipped,
            },
            "note": result.note,
            "mechanism_job_id": mechanism_job_id,
        },
        "rows": result.to_row_dicts(),
        "csv": result.to_csv_text(),
        "logs": result.logs,
        "mechanism_job_id": mechanism_job_id,
        # 兼容旧前端字段：异步后初值为空
        "mechanism_md": "",
        "mechanism_pdf_base64": "",
        "mechanism_pdf_name": "",
    }


@app.get("/")
async def index() -> RedirectResponse:
    return RedirectResponse(url="/static/index.html")


@app.get("/favicon.ico")
async def favicon() -> Response:
    icon = STATIC_DIR / "favicon.png"
    if icon.is_file():
        return FileResponse(icon, media_type="image/png")
    return Response(status_code=204)


@app.get("/health")
def health() -> dict:
    cfg = load_config(mode="auto")
    return {
        "status": "ok",
        "version": "0.1.0",
        "build": _API_BUILD_MARK,
        "config_hash": cfg.config_hash,
        "mode": cfg.mode,
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
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.post("/api/screen")
async def screen_endpoint(
    file: UploadFile = File(...),
    top: int = 10,
    mode: str = "auto",
    use_snapshot: bool = True,
) -> dict:
    top = _clamp_top(top)
    resolved_mode = _resolve_mode(mode)

    if not file.filename or not file.filename.lower().endswith(".sdf"):
        raise HTTPException(status_code=400, detail="请上传 .sdf 文件")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件为空")

    with tempfile.NamedTemporaryFile(suffix=".sdf", delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        cfg = load_config(mode=resolved_mode)
        _apply_snapshot_flag(cfg, use_snapshot)
        result = screen_sdf(tmp_path, cfg=cfg, top_n=top, source_filename=file.filename)
        job_id = _start_mechanism_async(result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    return _result_payload(result, mechanism_job_id=job_id)


@app.post("/api/screen/stream")
async def screen_stream(
    file: UploadFile = File(...),
    top: int = 10,
    mode: str = "auto",
    use_snapshot: bool = True,
) -> StreamingResponse:
    """NDJSON 流：筛选日志 + result（含 mechanism_job_id）；PDF 后台生成。"""
    top = _clamp_top(top)
    resolved_mode = _resolve_mode(mode)

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
            cfg = load_config(mode=resolved_mode)
            _apply_snapshot_flag(cfg, use_snapshot)
            result = screen_sdf(
                tmp_path,
                cfg=cfg,
                top_n=top,
                source_filename=filename,
                log_sink=on_log,
            )
            job_id = _start_mechanism_async(result, log_sink=on_log)
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"type": "result", **_result_payload(result, mechanism_job_id=job_id)},
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


@app.post("/api/screen/download")
async def screen_download(
    file: UploadFile = File(...),
    top: int = 10,
    mode: str = "auto",
    use_snapshot: bool = True,
) -> Response:
    payload = await screen_endpoint(
        file=file, top=top, mode=mode, use_snapshot=use_snapshot
    )
    return Response(
        content=payload["csv"],
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=nomination_top10.csv"},
    )
