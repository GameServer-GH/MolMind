"""Agent API：会话、上传、流式对话、产物下载、设置/Catalog。"""

from __future__ import annotations

import asyncio
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from agent import get_runtime
from agent.runtime.loop import SessionBusyError
from apps.api.download_headers import content_disposition_attachment
from plugins.molmind_core.scientific.pipeline.runner import TOP_N_MAX, TOP_N_MIN

router = APIRouter(prefix="/api/agent", tags=["agent"])
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent")

# 默认试用库：data/T001 TargetMol现货产品22966.sdf（完整参考库）。
# 可用 MOLMIND_DEMO_SDF 覆盖路径。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEMO_SDF_FILENAME = "T001 TargetMol现货产品22966.sdf"
_DEFAULT_DEMO_SDF = _REPO_ROOT / "data" / _DEMO_SDF_FILENAME
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def _require_client_id(
    x_molmind_client_id: Optional[str] = Header(default=None),
    client_id: Optional[str] = Query(default=None),
) -> str:
    """Resolve the browser owner id (query fallback is for direct downloads)."""
    value = (x_molmind_client_id or client_id or "").strip()
    if not _CLIENT_ID_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail="缺少或无效的浏览器客户端标识")
    return value


def _owned_session(session_id: str, client_id: str):
    session = get_runtime().get_session(session_id)
    # Deliberately return the same result for missing and foreign sessions so
    # an installation id cannot be used to enumerate another browser's data.
    if not session or session.client_id != client_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


def _busy_conflict(exc: SessionBusyError) -> HTTPException:
    return HTTPException(status_code=409, detail=exc.payload)


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


class ToolApprovalBody(BaseModel):
    tool_id: str = Field(..., min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    ttl_sec: int = Field(default=600, ge=30, le=3600)


class ClientIdentityLookupBody(BaseModel):
    client_id: str = Field(..., min_length=16, max_length=128)


@router.get("/settings")
def get_settings(
    session_id: Optional[str] = None,
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    runtime = get_runtime()
    session = _owned_session(session_id, client_id) if session_id else None
    return runtime.settings_view(session)


@router.post("/sessions")
def create_session(
    profile_id: str = "competition_masld",
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    runtime = get_runtime()
    try:
        runtime.registry.get_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session = runtime.create_session(profile_id=profile_id, client_id=client_id)
    settings = runtime.settings_view(session)
    return {
        "session_id": session.session_id,
        "profile": session.profile_id,
        "skills": settings.get("enabled_skills") or [],
        "builtin_plugins": settings.get("builtin_plugins") or [],
        "catalog": settings.get("catalog") or [],
    }


@router.post("/clients/validate")
def validate_client_identity(
    body: ClientIdentityLookupBody,
    _current_client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    target = body.client_id.strip()
    if not _CLIENT_ID_RE.fullmatch(target):
        raise HTTPException(status_code=400, detail="用户 ID 格式无效")
    if not get_runtime().store.client_exists(target):
        raise HTTPException(status_code=404, detail="未找到该用户记录")
    latest = get_runtime().store.list_sessions(limit=1, client_id=target)
    return {
        "client_id": target,
        "exists": True,
        "latest_session_id": latest[0]["session_id"] if latest else None,
    }


@router.post("/clients/register")
def register_client_identity(
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    get_runtime().store.register_client(client_id)
    return {"client_id": client_id, "registered": True}


@router.get("/sessions")
def list_sessions(
    limit: int = 50,
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    runtime = get_runtime()
    items = runtime.store.list_sessions(limit=limit, client_id=client_id)
    return {"sessions": items, "count": len(items)}


@router.delete("/sessions")
def clear_sessions(client_id: str = Depends(_require_client_id)) -> dict[str, Any]:
    """Clear only the current browser installation's conversation history."""
    try:
        deleted_count = get_runtime().clear_sessions_when_idle(client_id=client_id)
    except SessionBusyError as exc:
        raise _busy_conflict(exc) from exc
    return {"deleted_count": deleted_count}


@router.get("/sessions/{session_id}")
def get_session(
    session_id: str,
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    session = _owned_session(session_id, client_id)
    return {
        "session_id": session.session_id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "profile_id": session.profile_id,
        "title": session.title or None,
        "sdf_filename": session.sdf_filename or None,
        "has_sdf": bool(session.sdf_bytes),
        "sdf_ui_pending": bool(session.sdf_ui_pending) and bool(session.sdf_bytes),
        "top_n": session.top_n,
        "pending_action": session.pending_action,
        "pending_goal": session.pending_goal,
        "last_run_id": session.last_run_id or None,
        "last_selection_sha256": session.last_selection_sha256 or None,
        "run_history": list(session.run_history),
        "active_plan": session.active_plan,
        "plan_history": list(session.plan_history),
        "working_memory": list(session.working_memory),
        "agent_run_state": session.agent_run_state,
        "approvals": [
            {
                key: record.get(key)
                for key in (
                    "approval_id",
                    "tool_id",
                    "scope",
                    "args_hash",
                    "decision",
                    "expires_at",
                    "used_at",
                )
            }
            for record in session.approval_grants
            if isinstance(record, dict)
        ],
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
        "active_run": session.active_run,
        "revision": session.revision,
    }


@router.get("/sessions/{session_id}/events")
def get_session_events(
    session_id: str,
    after_seq: int = 0,
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    runtime = get_runtime()
    session = _owned_session(session_id, client_id)
    events = runtime.store.read_events(session_id, after_seq=after_seq)
    return {
        "session_id": session_id,
        "events": events,
        "event_seq": session.event_seq,
        "active_run": session.active_run,
    }


@router.patch("/sessions/{session_id}")
def patch_session(
    session_id: str,
    body: SessionPatchBody,
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    runtime = get_runtime()
    session = _owned_session(session_id, client_id)
    runtime.rename_session(session, body.title)
    return {"session_id": session_id, "title": session.title}


@router.post("/sessions/{session_id}/approvals")
def approve_tool_call(
    session_id: str,
    body: ToolApprovalBody,
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    runtime = get_runtime()
    session = _owned_session(session_id, client_id)
    try:
        approval = runtime.grant_tool_approval(
            session,
            tool_id=body.tool_id,
            args=body.args,
            ttl_sec=body.ttl_sec,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "session_id": session_id,
        "approval": {
            key: approval.get(key)
            for key in (
                "approval_id",
                "tool_id",
                "scope",
                "args_hash",
                "decision",
                "expires_at",
            )
        },
    }


@router.delete("/sessions/{session_id}")
def delete_session(
    session_id: str,
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    runtime = get_runtime()
    _owned_session(session_id, client_id)
    try:
        ok = runtime.delete_session_when_idle(session_id)
    except SessionBusyError as exc:
        raise _busy_conflict(exc) from exc
    if not ok:
        # also treat missing as gone
        if runtime.get_session(session_id):
            raise HTTPException(status_code=500, detail="删除失败")
        # if already absent on disk, still 200 for idempotent UI
    return {"session_id": session_id, "deleted": True}


@router.post("/sessions/{session_id}/upload")
async def upload_sdf(
    session_id: str,
    file: UploadFile = File(...),
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    runtime = get_runtime()
    session = _owned_session(session_id, client_id)
    if not file.filename or not file.filename.lower().endswith(".sdf"):
        raise HTTPException(status_code=400, detail="请上传 .sdf 文件")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件为空")
    try:
        session = runtime.attach_session_sdf(
            session_id,
            filename=file.filename,
            content=content,
        )
    except SessionBusyError as exc:
        raise _busy_conflict(exc) from exc
    return {
        "session_id": session_id,
        "sdf_filename": session.sdf_filename,
        "size_bytes": len(content),
        "has_sdf": True,
        "sdf_ui_pending": True,
    }


@router.delete("/sessions/{session_id}/upload")
def clear_sdf(
    session_id: str,
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    runtime = get_runtime()
    session = _owned_session(session_id, client_id)
    try:
        session = runtime.detach_session_sdf(session_id)
    except SessionBusyError as exc:
        raise _busy_conflict(exc) from exc
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
def attach_demo_sdf(
    session_id: str,
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    """将内置试用 SDF 绑定为当前会话附件（服务端拷贝，无需浏览器重传）。"""
    runtime = get_runtime()
    session = _owned_session(session_id, client_id)
    path = _demo_sdf_path()
    content = path.read_bytes()
    if not content:
        raise HTTPException(status_code=400, detail="试用样例库为空")
    try:
        session = runtime.attach_session_sdf(
            session_id,
            filename=path.name,
            content=content,
        )
    except SessionBusyError as exc:
        raise _busy_conflict(exc) from exc
    return {
        "session_id": session_id,
        "sdf_filename": session.sdf_filename,
        "size_bytes": len(content),
        "has_sdf": True,
        "sdf_ui_pending": True,
    }


@router.post("/sessions/{session_id}/catalog/install")
def catalog_install(
    session_id: str,
    body: CatalogBody,
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    runtime = get_runtime()
    session = _owned_session(session_id, client_id)
    try:
        session = runtime.install_session_catalog(session_id, body.plugin_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SessionBusyError as exc:
        raise _busy_conflict(exc) from exc
    return {
        "session_id": session_id,
        "installed_catalog": list(session.installed_catalog),
        "settings": runtime.settings_view(session),
    }


@router.delete("/sessions/{session_id}/catalog/{plugin_id}")
def catalog_uninstall(
    session_id: str,
    plugin_id: str,
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    runtime = get_runtime()
    session = _owned_session(session_id, client_id)
    try:
        session = runtime.uninstall_session_catalog(session_id, plugin_id)
    except SessionBusyError as exc:
        raise _busy_conflict(exc) from exc
    return {
        "session_id": session_id,
        "installed_catalog": list(session.installed_catalog),
        "settings": runtime.settings_view(session),
    }


@router.post("/sessions/{session_id}/message/stream")
async def message_stream(
    session_id: str,
    body: MessageBody,
    client_id: str = Depends(_require_client_id),
) -> StreamingResponse:
    runtime = get_runtime()
    session = _owned_session(session_id, client_id)
    if body.top_n is not None:
        if body.top_n < TOP_N_MIN or body.top_n > TOP_N_MAX:
            raise HTTPException(
                status_code=400,
                detail=f"top_n 须在 {TOP_N_MIN}–{TOP_N_MAX} 之间",
            )

    try:
        reserved_run = runtime.reserve_session_run(
            session_id,
            body.text,
            top_n=body.top_n,
        )
    except SessionBusyError as exc:
        raise _busy_conflict(exc) from exc
    run_id = str(reserved_run["run_id"])

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    def run_job() -> None:
        try:
            for event in runtime.handle_reserved_session_message(
                session_id,
                run_id,
                body.text,
                top_n=body.top_n,
            ):
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as exc:  # noqa: BLE001
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"type": "error", "detail": str(exc)},
            )
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "done"})
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    # The durable Run owns execution, not the lifetime of the HTTP response.
    # Submitting before StreamingResponse iteration lets refresh/disconnect
    # detach the observer without leaving a permanently queued Run.
    _EXECUTOR.submit(run_job)

    async def generate():
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
            "X-MolMind-Run-ID": run_id,
        },
    )


@router.get("/sessions/{session_id}/artifacts/{artifact_id}/download")
def download_artifact(
    session_id: str,
    artifact_id: str,
    client_id: str = Depends(_require_client_id),
) -> Response:
    session = _owned_session(session_id, client_id)
    art = session.artifacts.get(artifact_id)
    if not art:
        raise HTTPException(status_code=404, detail="产物不存在")
    headers = {
        "Content-Disposition": content_disposition_attachment(art.filename),
    }
    return Response(content=art.content, media_type=art.media_type, headers=headers)
