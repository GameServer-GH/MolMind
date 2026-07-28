"""Agent API：会话、上传、流式对话、产物下载、设置/Catalog。"""

from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from agent import get_runtime
from apps.api.download_headers import content_disposition_attachment
from plugins.molmind_core.scientific.pipeline.runner import TOP_N_MAX, TOP_N_MIN

router = APIRouter(prefix="/api/agent", tags=["agent"])
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent")

# 默认试用库：data/T001 TargetMol现货产品22966.sdf（完整参考库）。
# 可用 MOLMIND_DEMO_SDF 覆盖路径。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEMO_SDF_FILENAME = "T001 TargetMol现货产品22966.sdf"
_DEFAULT_DEMO_SDF = _REPO_ROOT / "data" / _DEMO_SDF_FILENAME


def _resolve_demo_sdf() -> tuple[Path, str]:
    override = (os.environ.get("MOLMIND_DEMO_SDF") or "").strip()
    if override:
        path = Path(override).expanduser().resolve()
    else:
        path = _DEFAULT_DEMO_SDF
    return path, path.name


def _demo_sdf_path() -> Path:
    path, name = _resolve_demo_sdf()
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                f"试用样例库未找到：{path}。"
                f"请确认 data/{_DEMO_SDF_FILENAME} 存在，或设置环境变量 MOLMIND_DEMO_SDF。"
            ),
        )
    return path


class MessageBody(BaseModel):
    text: str = Field(..., min_length=1)
    top_n: Optional[int] = None


class CatalogBody(BaseModel):
    plugin_id: str = Field(..., min_length=1)


class SessionPatchBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=80)


@router.get("/settings")
def get_settings(session_id: Optional[str] = None) -> dict[str, Any]:
    runtime = get_runtime()
    session = runtime.get_session(session_id) if session_id else None
    return runtime.settings_view(session)


@router.post("/sessions")
def create_session(profile_id: str = "competition_masld") -> dict[str, Any]:
    runtime = get_runtime()
    try:
        runtime.registry.get_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session = runtime.create_session(profile_id=profile_id)
    settings = runtime.settings_view(session)
    return {
        "session_id": session.session_id,
        "profile": session.profile_id,
        "skills": settings.get("enabled_skills") or [],
        "builtin_plugins": settings.get("builtin_plugins") or [],
        "catalog": settings.get("catalog") or [],
    }


@router.get("/sessions")
def list_sessions(limit: int = 50) -> dict[str, Any]:
    runtime = get_runtime()
    items = runtime.store.list_sessions(limit=limit)
    return {"sessions": items, "count": len(items)}


@router.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    session = get_runtime().get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {
        "session_id": session.session_id,
        "profile_id": session.profile_id,
        "title": session.title or None,
        "sdf_filename": session.sdf_filename or None,
        "has_sdf": bool(session.sdf_bytes),
        "sdf_ui_pending": bool(session.sdf_ui_pending) and bool(session.sdf_bytes),
        "top_n": session.top_n,
        "last_run_id": session.last_run_id or None,
        "last_selection_sha256": session.last_selection_sha256 or None,
        "artifact_ids": list(session.artifacts.keys()),
        "artifacts": [
            {
                "artifact_id": a.artifact_id,
                "kind": a.kind,
                "title": a.title,
                "subtitle": a.subtitle,
                "filename": a.filename,
                "download_url": (
                    f"/api/agent/sessions/{session.session_id}/artifacts/"
                    f"{a.artifact_id}/download"
                ),
            }
            for a in session.artifacts.values()
        ],
        "messages": session.messages,
        "installed_catalog": list(session.installed_catalog),
        "event_seq": session.event_seq,
    }


@router.get("/sessions/{session_id}/events")
def get_session_events(session_id: str, after_seq: int = 0) -> dict[str, Any]:
    runtime = get_runtime()
    session = runtime.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    events = runtime.store.read_events(session_id, after_seq=after_seq)
    return {"session_id": session_id, "events": events, "event_seq": session.event_seq}


@router.patch("/sessions/{session_id}")
def patch_session(session_id: str, body: SessionPatchBody) -> dict[str, Any]:
    runtime = get_runtime()
    session = runtime.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    runtime.rename_session(session, body.title)
    return {"session_id": session_id, "title": session.title}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str) -> dict[str, Any]:
    runtime = get_runtime()
    ok = runtime.delete_session(session_id)
    if not ok:
        # also treat missing as gone
        if runtime.get_session(session_id):
            raise HTTPException(status_code=500, detail="删除失败")
        # if already absent on disk, still 200 for idempotent UI
    return {"session_id": session_id, "deleted": True}


@router.post("/sessions/{session_id}/upload")
async def upload_sdf(session_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    runtime = get_runtime()
    session = runtime.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if not file.filename or not file.filename.lower().endswith(".sdf"):
        raise HTTPException(status_code=400, detail="请上传 .sdf 文件")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件为空")
    runtime.attach_sdf(session, filename=file.filename, content=content)
    return {
        "session_id": session_id,
        "sdf_filename": session.sdf_filename,
        "size_bytes": len(content),
        "has_sdf": True,
        "sdf_ui_pending": True,
    }


@router.delete("/sessions/{session_id}/upload")
def clear_sdf(session_id: str) -> dict[str, Any]:
    runtime = get_runtime()
    session = runtime.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    runtime.detach_sdf(session)
    return {
        "session_id": session_id,
        "sdf_filename": None,
        "has_sdf": False,
        "sdf_ui_pending": False,
    }


@router.get("/demo/sdf")
def download_demo_sdf() -> FileResponse:
    """下载内置试用化合物库（默认 data/T001…sdf）。"""
    path = _demo_sdf_path()
    name = path.name
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={"Content-Disposition": content_disposition_attachment(name)},
    )


@router.get("/demo/sdf/info")
def demo_sdf_info() -> dict[str, Any]:
    """前端弹窗展示用：文件名与是否可用。"""
    path, name = _resolve_demo_sdf()
    ok = path.is_file()
    size = path.stat().st_size if ok else 0
    return {
        "filename": name,
        "available": ok,
        "size_bytes": size,
        "source": "env" if (os.environ.get("MOLMIND_DEMO_SDF") or "").strip() else f"data/{_DEMO_SDF_FILENAME}",
    }


@router.post("/sessions/{session_id}/demo-sdf")
def attach_demo_sdf(session_id: str) -> dict[str, Any]:
    """将内置试用 SDF 绑定为当前会话附件（服务端拷贝，无需浏览器重传）。"""
    runtime = get_runtime()
    session = runtime.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    path = _demo_sdf_path()
    content = path.read_bytes()
    if not content:
        raise HTTPException(status_code=400, detail="试用样例库为空")
    runtime.attach_sdf(session, filename=path.name, content=content)
    return {
        "session_id": session_id,
        "sdf_filename": session.sdf_filename,
        "size_bytes": len(content),
        "has_sdf": True,
        "sdf_ui_pending": True,
    }


@router.post("/sessions/{session_id}/catalog/install")
def catalog_install(session_id: str, body: CatalogBody) -> dict[str, Any]:
    runtime = get_runtime()
    session = runtime.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    try:
        runtime.install_catalog_plugin(session, body.plugin_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "session_id": session_id,
        "installed_catalog": list(session.installed_catalog),
        "settings": runtime.settings_view(session),
    }


@router.delete("/sessions/{session_id}/catalog/{plugin_id}")
def catalog_uninstall(session_id: str, plugin_id: str) -> dict[str, Any]:
    runtime = get_runtime()
    session = runtime.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    runtime.uninstall_catalog_plugin(session, plugin_id)
    return {
        "session_id": session_id,
        "installed_catalog": list(session.installed_catalog),
        "settings": runtime.settings_view(session),
    }


@router.post("/sessions/{session_id}/message/stream")
async def message_stream(session_id: str, body: MessageBody) -> StreamingResponse:
    runtime = get_runtime()
    session = runtime.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if body.top_n is not None:
        if body.top_n < TOP_N_MIN or body.top_n > TOP_N_MAX:
            raise HTTPException(
                status_code=400,
                detail=f"top_n 须在 {TOP_N_MIN}–{TOP_N_MAX} 之间",
            )
        session.top_n = body.top_n

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    def run_job() -> None:
        try:
            for event in runtime.handle_message(session, body.text):
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as exc:  # noqa: BLE001
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"type": "error", "detail": str(exc)},
            )
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "done"})
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    async def generate():
        _EXECUTOR.submit(run_job)
        while True:
            item = await queue.get()
            if item is None:
                break
            yield json.dumps(item, ensure_ascii=False) + "\n"
            # Yield to event loop so chunks flush to the client promptly.
            await asyncio.sleep(0)

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/sessions/{session_id}/artifacts/{artifact_id}/download")
def download_artifact(session_id: str, artifact_id: str) -> Response:
    session = get_runtime().get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    art = session.artifacts.get(artifact_id)
    if not art:
        raise HTTPException(status_code=404, detail="产物不存在")
    headers = {
        "Content-Disposition": content_disposition_attachment(art.filename),
    }
    return Response(content=art.content, media_type=art.media_type, headers=headers)
