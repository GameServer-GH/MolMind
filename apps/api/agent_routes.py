"""Agent API：会话、上传、流式对话、产物下载、设置/Catalog。"""

from __future__ import annotations

import asyncio
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from agent import get_runtime
from agent.runtime.loop import SessionBusyError, TurnQueueFullError
from agent.runtime.run_queue import RunJob, RunQueue, RunQueueWorkers, build_run_queue
from apps.api.download_headers import content_disposition_attachment
from plugins.molmind_core.scientific.pipeline.runner import TOP_N_MAX, TOP_N_MIN
from plugins.scp_hub.catalog import SCPCatalog
from plugins.scp_hub.client import MCPError

router = APIRouter(prefix="/api/agent", tags=["agent"])
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent")
_RUN_QUEUE: RunQueue | None = None
_RUN_WORKERS: RunQueueWorkers | None = None

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


def _enrich_pending_turns(session) -> list[dict[str, Any]]:
    """Attach filename/kind summaries so the queue rail can render chips."""
    enriched: list[dict[str, Any]] = []
    staged = session.staged_attachments if session else {}
    for item in list(session.pending_turns or []):
        if not isinstance(item, dict):
            continue
        row = dict(item)
        attachments: list[dict[str, Any]] = []
        for raw_id in item.get("attachment_ids") or []:
            attachment_id = str(raw_id or "")
            if not attachment_id:
                continue
            meta = staged.get(attachment_id) if isinstance(staged, dict) else None
            if not isinstance(meta, dict):
                attachments.append({"attachment_id": attachment_id, "filename": "", "kind": ""})
                continue
            attachments.append(
                {
                    "attachment_id": attachment_id,
                    "filename": str(meta.get("filename") or ""),
                    "kind": str(meta.get("kind") or ""),
                    "size": int(meta.get("size") or 0),
                }
            )
        row["attachments"] = attachments
        enriched.append(row)
    return enriched


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
                f"可选试用样例库未找到：{path}。"
                f"请确认 data/{_DEMO_SDF_FILENAME} 存在，或设置环境变量 MOLMIND_DEMO_SDF。"
            ),
        )
    return path


class MessageBody(BaseModel):
    text: str = Field(..., min_length=1)
    top_n: Optional[int] = None
    # Idle /message/stream must accept turn-scoped chips the same way /turns does;
    # otherwise demo/upload staging never binds into the session library.
    attachment_ids: list[str] = Field(default_factory=list, max_length=8)


class TurnBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=100_000)
    mode: str = Field(default="auto", pattern="^(auto|queue|guidance|run_now)$")
    attachment_ids: list[str] = Field(default_factory=list, max_length=8)
    idempotency_key: str = Field(default="", max_length=128)
    top_n: Optional[int] = None


class TurnPatchBody(BaseModel):
    text: Optional[str] = Field(default=None, min_length=1, max_length=100_000)
    attachment_ids: Optional[list[str]] = Field(default=None, max_length=8)


class TurnOrderBody(BaseModel):
    turn_ids: list[str] = Field(default_factory=list, max_length=3)


class CatalogBody(BaseModel):
    plugin_id: str = Field(..., min_length=1)

class SCPSkillBody(BaseModel):
    skill_id: str = Field(..., min_length=1)

class SCPCallBody(BaseModel):
    tool_id: str = Field(..., min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    # None inherits the owning plugin's network policy. Explicit false remains
    # a per-call offline override.
    allow_live: Optional[bool] = None
    force_refresh: bool = False
    stage: bool = False
    molecule_id: str = ""


class SessionPatchBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=80)


class ToolApprovalBody(BaseModel):
    tool_id: str = Field(..., min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    ttl_sec: int = Field(default=600, ge=30, le=3600)


class ClientIdentityLookupBody(BaseModel):
    client_id: str = Field(..., min_length=16, max_length=128)


def _run_background_chain(
    runtime,
    session_id: str,
    run_id: str,
    text: str,
    top_n: int | None,
) -> None:
    """Execute one Run and then drain durable queued Turns for the session."""
    current_run_id = run_id
    current_text = text
    current_top_n = top_n
    while current_run_id:
        for _event in runtime.handle_reserved_session_message(
            session_id,
            current_run_id,
            current_text,
            top_n=current_top_n,
        ):
            pass
        next_run = runtime.activate_next_queued_turn(session_id)
        if not next_run:
            break
        current_run_id = str(next_run.get("run_id") or "")
        current_text = str((next_run.get("input") or {}).get("text") or "")
        current_top_n = (next_run.get("input") or {}).get("top_n")


def _run_one_reserved(
    runtime,
    session_id: str,
    run_id: str,
    text: str,
    top_n: int | None,
) -> None:
    """Execute exactly one reserved Run; leave queue drain to the client/recovery."""
    for _event in runtime.handle_reserved_session_message(
        session_id,
        run_id,
        text,
        top_n=top_n,
    ):
        pass


def _run_leased_job(runtime, job: RunJob) -> None:
    """Execute exactly one leased Run; leave successors to client/recovery."""
    runtime.store.lease_managed = True
    payload = dict(job.payload or {})
    for _event in runtime.handle_reserved_session_message(
        job.session_id,
        job.run_id,
        str(payload.get("text") or ""),
        top_n=payload.get("top_n"),
    ):
        pass


def _enqueue_reserved_run(runtime, session_id: str, run: dict[str, Any]) -> None:
    run_id = str(run.get("run_id") or "")
    payload = {
        "text": str((run.get("input") or {}).get("text") or ""),
        "top_n": (run.get("input") or {}).get("top_n"),
        "retry_of_run_id": str(run.get("retry_of_run_id") or ""),
    }
    if _RUN_QUEUE is not None:
        _RUN_QUEUE.enqueue(run_id=run_id, session_id=session_id, payload=payload)
        return
    _EXECUTOR.submit(
        _run_one_reserved,
        runtime,
        session_id,
        run_id,
        payload["text"],
        payload["top_n"],
    )


def start_run_queue_workers() -> RunQueueWorkers:
    """Start lease-based workers. Safe to call once from FastAPI lifespan."""
    global _RUN_QUEUE, _RUN_WORKERS
    if _RUN_WORKERS is not None:
        return _RUN_WORKERS
    runtime = get_runtime()
    runtime.store.lease_managed = True
    _RUN_QUEUE = build_run_queue(runs_root=runtime.store.root)
    _RUN_WORKERS = RunQueueWorkers(
        _RUN_QUEUE,
        lambda job: _run_leased_job(runtime, job),
        cancel_handler=lambda job, reason: runtime.request_external_run_interrupt(
            job.session_id,
            job.run_id,
            reason=reason,
        ),
        workers=int(os.environ.get("MOLMIND_AGENT_WORKERS") or 2),
        lease_seconds=int(os.environ.get("MOLMIND_AGENT_LEASE_SECONDS") or 60),
    )
    recover_pending_sessions()
    recover_background_jobs()
    _RUN_WORKERS.start()
    return _RUN_WORKERS


def stop_run_queue_workers() -> None:
    global _RUN_QUEUE, _RUN_WORKERS
    if _RUN_WORKERS is not None:
        _RUN_WORKERS.stop()
    _RUN_WORKERS = None
    _RUN_QUEUE = None


def _schedule_pending_if_idle(runtime, session_id: str) -> None:
    next_run = runtime.activate_next_queued_turn(session_id)
    if not next_run:
        return
    _enqueue_reserved_run(runtime, session_id, next_run)


def recover_pending_sessions() -> int:
    """Resume durable queued Turns after a process restart."""
    runtime = get_runtime()
    recovered = 0
    for item in runtime.store.list_sessions(limit=10_000):
        session_id = str(item.get("session_id") or "")
        if not session_id:
            continue
        try:
            session = runtime.get_session(session_id)
        except Exception:
            # A single corrupt/legacy session must not block API startup.
            continue
        if session and runtime._run_is_active(session.active_run):
            active = dict(session.active_run or {})
            run_id = str(active.get("run_id") or "")
            if run_id and (_RUN_QUEUE is None or not _RUN_QUEUE.has_live_run(run_id)):
                _enqueue_reserved_run(runtime, session_id, active)
                recovered += 1
            continue
        if session and session.pending_turns and not runtime._run_is_active(session.active_run):
            _schedule_pending_if_idle(runtime, session_id)
            recovered += 1
    return recovered


def recover_background_jobs() -> dict[str, list[str]]:
    """Re-dispatch orphaned Mechanism/SCP background jobs after a crash."""
    from plugins.molmind_core.scientific.mechanism import jobs as mechanism_jobs

    stale_seconds = int(os.environ.get("MOLMIND_JOB_STALE_SECONDS") or 120)
    runtime = get_runtime()

    def _scp_call_factory(job: dict[str, Any]):
        session_id = str(job.get("session_id") or "")
        tool_id = str(job.get("tool_id") or "")
        arguments = dict(job.get("arguments") or {})
        allow_live = bool(job.get("allow_live", True))
        force_refresh = bool(job.get("force_refresh", False))
        session = runtime.get_session(session_id) if session_id else None
        if session is None:
            raise RuntimeError("session_missing_for_scp_recovery")

        def _call():
            return runtime.scp.call(
                session,
                tool_id,
                arguments,
                allow_live=allow_live,
                force_refresh=force_refresh,
            )

        return _call

    mechanism_ids = mechanism_jobs.recover_orphan_jobs(stale_seconds=stale_seconds)
    scp_ids = runtime.scp_jobs.recover_orphan_jobs(
        call_factory=_scp_call_factory,
        stale_seconds=stale_seconds,
    )
    return {"mechanism_pdf": mechanism_ids, "scp_tool": scp_ids}


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
    # Do not auto-activate pending turns here. The live UI promotes via
    # POST /turns/next after streaming settles; process restart recovery uses
    # recover_pending_sessions().
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
        "installed_scp_skills": list(session.installed_scp_skills.values()),
        "event_seq": session.event_seq,
        "active_run": session.active_run,
        "pending_turns": _enrich_pending_turns(session),
        "queue_count": sum(
            1 for item in session.pending_turns if item.get("kind") != "guidance"
        ),
        "queue_limit": 3,
        "staged_attachments": list(session.staged_attachments.values()),
        "agent_run_history": list(session.agent_run_history),
        "tool_checkpoints": list(session.tool_checkpoints),
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


@router.get("/sessions/{session_id}/events/stream")
async def stream_session_events(
    session_id: str,
    request: Request,
    after_seq: int = 0,
    client_id: str = Depends(_require_client_id),
) -> StreamingResponse:
    """Reconnectable session-level SSE stream spanning queued Run boundaries."""
    runtime = get_runtime()
    _owned_session(session_id, client_id)

    async def generate():
        cursor = max(0, int(after_seq))
        quiet_ticks = 0
        while not await request.is_disconnected():
            session = runtime.get_session(session_id)
            if session is None or session.client_id != client_id:
                break
            events = runtime.store.read_events(session_id, after_seq=cursor)
            if events:
                quiet_ticks = 0
                for event in events:
                    cursor = max(cursor, int(event.get("seq") or 0))
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            else:
                quiet_ticks += 1
                if quiet_ticks % 40 == 0:
                    yield f": heartbeat {cursor}\n\n"
            await asyncio.sleep(0.25)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


@router.post("/sessions/{session_id}/turn-attachments")
async def stage_turn_attachment(
    session_id: str,
    file: UploadFile = File(...),
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    """Upload a Turn-owned attachment without mutating an active Run."""
    from agent.memory.attachments import (
        guess_media_type,
        is_allowed_attachment_filename,
    )

    runtime = get_runtime()
    session = _owned_session(session_id, client_id)
    if not file.filename or not is_allowed_attachment_filename(file.filename):
        raise HTTPException(
            status_code=400,
            detail="支持的附件类型：.sdf / .pdf / 图片 / .txt .md .csv .json / .docx",
        )
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(content) > 500 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="单个附件不能超过 500 MB")
    try:
        metadata = runtime.store.stage_attachment(
            session,
            filename=file.filename,
            content=content,
            media_type=file.content_type
            or guess_media_type(file.filename, "application/octet-stream"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"session_id": session_id, "attachment": metadata}


@router.delete("/sessions/{session_id}/turn-attachments/{attachment_id}")
def delete_turn_attachment(
    session_id: str,
    attachment_id: str,
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    runtime = get_runtime()
    session = _owned_session(session_id, client_id)
    if not runtime.store.delete_staged_attachment(session, attachment_id):
        raise HTTPException(status_code=409, detail="附件已进入执行或不存在")
    return {"session_id": session_id, "attachment_id": attachment_id, "deleted": True}


@router.post("/sessions/{session_id}/turns")
def submit_turn(
    session_id: str,
    body: TurnBody,
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    runtime = get_runtime()
    _owned_session(session_id, client_id)
    if body.top_n is not None and not (TOP_N_MIN <= body.top_n <= TOP_N_MAX):
        raise HTTPException(
            status_code=400,
            detail=f"top_n 须在 {TOP_N_MIN}–{TOP_N_MAX} 之间",
        )
    try:
        accepted = runtime.submit_session_turn(
            session_id,
            body.text,
            mode=body.mode,
            attachment_ids=body.attachment_ids,
            idempotency_key=body.idempotency_key,
            top_n=body.top_n,
        )
    except (SessionBusyError, TurnQueueFullError) as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if accepted.get("disposition") == "started" and not accepted.get("duplicate"):
        _enqueue_reserved_run(runtime, session_id, accepted)
    if accepted.get("disposition") == "guidance" and _RUN_QUEUE is not None:
        _RUN_QUEUE.request_cancel(
            str(accepted.get("parent_run_id") or ""),
            reason="user_guidance",
        )
    session = runtime.get_session(session_id)
    return {
        "accepted": True,
        **accepted,
        "queue_count": runtime._normal_queue_size(session) if session else 0,
        "queue_limit": 3,
    }


@router.post("/sessions/{session_id}/runs/{run_id}/retry")
def retry_run(
    session_id: str,
    run_id: str,
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    runtime = get_runtime()
    _owned_session(session_id, client_id)
    try:
        retry = runtime.retry_session_run(session_id, run_id)
    except SessionBusyError as exc:
        raise _busy_conflict(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _enqueue_reserved_run(runtime, session_id, retry)
    return {"accepted": True, "disposition": "started", **retry}


@router.post("/sessions/{session_id}/runs/{run_id}/interrupt")
def interrupt_run(
    session_id: str,
    run_id: str,
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    """Hard-stop the active Run without enqueuing guidance."""
    runtime = get_runtime()
    _owned_session(session_id, client_id)
    try:
        result = runtime.interrupt_session_run(
            session_id,
            run_id,
            reason="user_stop",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if _RUN_QUEUE is not None:
        _RUN_QUEUE.request_cancel(str(result.get("run_id") or run_id), reason="user_stop")
    return {"accepted": True, **result}


@router.get("/sessions/{session_id}/turns")
def list_turns(
    session_id: str,
    schedule: bool = Query(default=False),
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    runtime = get_runtime()
    session = _owned_session(session_id, client_id)
    # Opt-in only: the live UI drains the queue after stream rendering finishes.
    if schedule:
        _schedule_pending_if_idle(runtime, session_id)
        session = runtime.get_session(session_id) or session
    return {
        "session_id": session_id,
        "active_run": session.active_run,
        "turns": _enrich_pending_turns(session),
        "queue_count": runtime._normal_queue_size(session),
        "queue_limit": 3,
    }


@router.post("/sessions/{session_id}/turns/next")
def start_next_turn(
    session_id: str,
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    """Promote at most one queued Turn after the previous UI stream has settled."""
    runtime = get_runtime()
    _owned_session(session_id, client_id)
    next_run = runtime.activate_next_queued_turn(session_id)
    if not next_run:
        session = runtime.get_session(session_id)
        return {
            "accepted": False,
            "started": False,
            "active_run": session.active_run if session else None,
            "turns": _enrich_pending_turns(session) if session else [],
            "queue_count": runtime._normal_queue_size(session) if session else 0,
            "queue_limit": 3,
        }
    _enqueue_reserved_run(runtime, session_id, next_run)
    session = runtime.get_session(session_id)
    return {
        "accepted": True,
        "started": True,
        "active_run": next_run,
        "turns": _enrich_pending_turns(session) if session else [],
        "queue_count": runtime._normal_queue_size(session) if session else 0,
        "queue_limit": 3,
    }


@router.delete("/sessions/{session_id}/turns/{turn_id}")
def cancel_turn(
    session_id: str,
    turn_id: str,
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    runtime = get_runtime()
    _owned_session(session_id, client_id)
    if not runtime.cancel_queued_turn(session_id, turn_id):
        raise HTTPException(status_code=409, detail="该消息已开始执行或不存在")
    session = runtime.get_session(session_id)
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "cancelled": True,
        "queue_count": runtime._normal_queue_size(session) if session else 0,
    }


@router.patch("/sessions/{session_id}/turns/{turn_id}")
def patch_turn(
    session_id: str,
    turn_id: str,
    body: TurnPatchBody,
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    runtime = get_runtime()
    _owned_session(session_id, client_id)
    try:
        turn = runtime.update_queued_turn(
            session_id,
            turn_id,
            text=body.text,
            attachment_ids=body.attachment_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if turn is None:
        raise HTTPException(status_code=409, detail="该消息已开始执行或不存在")
    return {"session_id": session_id, "turn": turn}


@router.put("/sessions/{session_id}/turns/order")
def reorder_turns(
    session_id: str,
    body: TurnOrderBody,
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    runtime = get_runtime()
    _owned_session(session_id, client_id)
    try:
        turns = runtime.reorder_queued_turns(session_id, body.turn_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"session_id": session_id, "turns": turns}


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
    stage: bool = Query(
        default=False,
        description="Force turn-scoped staging (for queueing while a run/UI is busy)",
    ),
    client_id: str = Depends(_require_client_id),
) -> dict[str, Any]:
    """将内置试用 SDF 绑定为当前会话附件（服务端拷贝，无需浏览器重传）。

    会话忙碌或显式 stage=1 时改为 turn 级暂存，以便随排队提示词一并发送。
    """
    runtime = get_runtime()
    session = _owned_session(session_id, client_id)
    path = _demo_sdf_path()
    content = path.read_bytes()
    if not content:
        raise HTTPException(status_code=400, detail="可选试用样例库为空")

    def _stage() -> dict[str, Any]:
        try:
            metadata = runtime.store.stage_attachment(
                session,
                filename=path.name,
                content=content,
                media_type="chemical/x-mdl-sdfile",
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "session_id": session_id,
            "attachment": metadata,
            "sdf_filename": metadata.get("filename") or path.name,
            "size_bytes": len(content),
            "has_sdf": False,
            "sdf_ui_pending": False,
            "staged": True,
        }

    if stage or runtime._run_is_active(session.active_run):
        return _stage()
    try:
        session = runtime.attach_session_sdf(
            session_id,
            filename=path.name,
            content=content,
        )
    except SessionBusyError:
        return _stage()
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

@router.get("/scp/catalog")
def scp_catalog(q: str = "", limit: int = Query(default=8, ge=1, le=50)) -> dict[str, Any]:
    catalog = SCPCatalog()
    return {"skills": catalog.search(q, limit=limit) if q.strip() else catalog.list()}

@router.get("/scp/skills/{skill_id}")
def scp_skill(skill_id: str) -> dict[str, Any]:
    try: return SCPCatalog().get(skill_id)
    except KeyError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc

@router.post("/sessions/{session_id}/scp/install")
def scp_install(session_id: str, body: SCPSkillBody, client_id: str = Depends(_require_client_id)) -> dict[str, Any]:
    runtime = get_runtime(); _owned_session(session_id, client_id)
    try: session = runtime.install_scp_skill(session_id, body.skill_id)
    except MCPError as exc: raise HTTPException(status_code=424, detail={"code": exc.code, "message": str(exc)}) from exc
    except (KeyError, ValueError) as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SessionBusyError as exc: raise _busy_conflict(exc) from exc
    return {"session_id": session_id, "skill": session.installed_scp_skills[body.skill_id], "settings": runtime.settings_view(session)}

@router.post("/sessions/{session_id}/scp/{skill_id}/enable")
def scp_enable(session_id: str, skill_id: str, client_id: str = Depends(_require_client_id)) -> dict[str, Any]:
    runtime = get_runtime(); _owned_session(session_id, client_id)
    try: session = runtime.set_scp_skill_enabled(session_id, skill_id, True)
    except KeyError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"skill": session.installed_scp_skills[skill_id]}

@router.post("/sessions/{session_id}/scp/{skill_id}/disable")
def scp_disable(session_id: str, skill_id: str, client_id: str = Depends(_require_client_id)) -> dict[str, Any]:
    runtime = get_runtime(); _owned_session(session_id, client_id)
    try: session = runtime.set_scp_skill_enabled(session_id, skill_id, False)
    except KeyError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"skill": session.installed_scp_skills[skill_id]}

@router.delete("/sessions/{session_id}/scp/{skill_id}")
def scp_uninstall(session_id: str, skill_id: str, client_id: str = Depends(_require_client_id)) -> dict[str, Any]:
    runtime = get_runtime(); _owned_session(session_id, client_id)
    try: session = runtime.uninstall_scp_skill(session_id, skill_id)
    except SessionBusyError as exc: raise _busy_conflict(exc) from exc
    return {"session_id": session_id, "installed_scp_skills": list(session.installed_scp_skills), "settings": runtime.settings_view(session)}

@router.post("/sessions/{session_id}/scp/tools/call")
def scp_call(session_id: str, body: SCPCallBody, client_id: str = Depends(_require_client_id)) -> dict[str, Any]:
    runtime = get_runtime(); session = _owned_session(session_id, client_id)
    plugin = runtime.registry.plugins.get("scp-hub")
    plugin_policy = getattr(plugin, "network_policy", {}) if plugin else {}
    allow_live = bool(body.allow_live) if body.allow_live is not None else bool(
        isinstance(plugin_policy, dict) and plugin_policy.get("default_live", False)
    )
    tool = runtime.registry.tools.get(body.tool_id)
    if tool and float(tool.timeout_sec or 0) > 120:
        skill_id = next((sid for sid, state in session.installed_scp_skills.items() if body.tool_id in state.get("tools", [])), "")
        job = runtime.scp_jobs.submit(lambda: runtime.scp.call(session, body.tool_id, body.arguments, allow_live=allow_live, force_refresh=body.force_refresh, stage=body.stage, molecule_id=body.molecule_id), session_id=session_id, skill_id=skill_id, tool_id=body.tool_id)
        return {"tool_id":body.tool_id,"status":"queued","job":job}
    try: observation = runtime.scp.call(session, body.tool_id, body.arguments, allow_live=allow_live, force_refresh=body.force_refresh, stage=body.stage, molecule_id=body.molecule_id)
    except MCPError as exc: raise HTTPException(status_code=502, detail={"code": exc.code, "message": str(exc)}) from exc
    except PermissionError as exc: raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"tool_id": body.tool_id, "observation": {**observation.__dict__, "content": [block.__dict__ for block in observation.content]}}

@router.get("/sessions/{session_id}/scp/staging")
def scp_staging(session_id: str, client_id: str = Depends(_require_client_id)) -> dict[str, Any]:
    runtime = get_runtime(); _owned_session(session_id, client_id)
    return {"items": runtime.scp.cache.list_staging(session_id=session_id)}

@router.get("/sessions/{session_id}/scp/calls")
def scp_calls(session_id: str, limit: int = Query(default=100, ge=1, le=500), client_id: str = Depends(_require_client_id)) -> dict[str, Any]:
    runtime = get_runtime(); _owned_session(session_id, client_id)
    return {"calls": runtime.scp.cache.list_calls(session_id=session_id, limit=limit)}

@router.get("/sessions/{session_id}/scp/jobs/{job_id}")
def scp_job(session_id: str, job_id: str, client_id: str = Depends(_require_client_id)) -> dict[str, Any]:
    runtime = get_runtime(); _owned_session(session_id, client_id)
    job = runtime.scp_jobs.get(job_id, session_id=session_id)
    if not job: raise HTTPException(status_code=404, detail="SCP Job 不存在")
    return job

@router.delete("/sessions/{session_id}/scp/jobs/{job_id}")
def cancel_scp_job(session_id: str, job_id: str, client_id: str = Depends(_require_client_id)) -> dict[str, Any]:
    runtime = get_runtime(); _owned_session(session_id, client_id)
    if not runtime.scp_jobs.cancel(job_id, session_id=session_id, reason="user_cancelled"):
        raise HTTPException(status_code=409, detail="SCP Job 已结束或不存在")
    return {"job_id": job_id, "status": "cancel_requested"}


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
            attachment_ids=body.attachment_ids,
        )
    except SessionBusyError as exc:
        raise _busy_conflict(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        # Do not auto-activate queued Turns here. The browser drains the queue
        # only after the previous turn's streaming UI has finished rendering.

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
