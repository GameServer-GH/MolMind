"""Canonical, bounded Observation envelopes for Agent tool results."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any

from agent.policy import claim_ceiling_default
from agent.runtime.scheduler import ScheduledCall, utc_now


def compact_text(value: object, *, limit: int = 1800) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    head = max(1, int(limit * 0.72))
    tail = max(1, limit - head - 24)
    return f"{text[:head]}\n…[中间内容已压缩]…\n{text[-tail:]}"


def _safe_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return {"summary": compact_text(value)}


@dataclass
class ToolObservation:
    task_id: str
    tool_id: str
    status: str
    ok: bool
    summary: str = ""
    digest: dict[str, Any] = field(default_factory=dict)
    data_ref: str = ""
    degraded_channels: list[str] = field(default_factory=list)
    error: dict[str, str] | None = None
    args_hash: str = ""
    claim_ceiling: str = field(default_factory=claim_ceiling_default)
    writes_selection: bool = False
    started_at: str = ""
    ended_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "tool_id": self.tool_id,
            "status": self.status,
            "ok": self.ok,
            "summary": self.summary,
            "data_ref": self.data_ref or None,
            "digest": dict(self.digest),
            "degraded_channels": list(self.degraded_channels),
            "error": dict(self.error) if self.error else None,
            "args_hash": self.args_hash,
            "claim_ceiling": self.claim_ceiling,
            "writes_selection": self.writes_selection,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }

    @property
    def signature(self) -> str:
        stable = {
            "tool_id": self.tool_id,
            "status": self.status,
            "digest": self.digest,
            "error": self.error,
        }
        encoded = json.dumps(
            stable,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def normalize_tool_end(
    event: dict[str, Any],
    *,
    call: ScheduledCall | None = None,
    observation_limit: int = 12_000,
) -> ToolObservation:
    ok = bool(event.get("ok"))
    explicit_status = str(event.get("status") or "")
    if explicit_status in {
        "succeeded",
        "failed",
        "denied",
        "timed_out",
        "cancelled",
        "degraded",
    }:
        status = explicit_status
    else:
        status = "succeeded" if ok else "failed"
    digest = _safe_mapping(event.get("digest"))
    digest_text = json.dumps(digest, ensure_ascii=False, default=str)
    if len(digest_text) > observation_limit:
        retained_keys = (
            "run_id",
            "artifact_id",
            "job_id",
            "output_count",
            "selection_sha256",
            "config_hash",
            "writes_selection",
            "selection_sha256_unchanged",
            "error_code",
            "degraded",
            "degraded_channels",
        )
        digest = {
            key: digest[key]
            for key in retained_keys
            if key in digest
        }
        digest.update(
            {
                "compacted": True,
                "original_chars": len(digest_text),
            }
        )
    degraded = event.get("degraded_channels")
    if degraded is None:
        degraded = digest.get("degraded") or digest.get("degraded_channels") or []
    degraded_channels = [str(value) for value in degraded or [] if str(value)]
    if ok and degraded_channels and status == "succeeded":
        status = "degraded"

    raw_error = compact_text(event.get("error") or "", limit=min(1200, observation_limit))
    error = None
    if raw_error or not ok:
        error = {
            "code": str(event.get("error_code") or ("tool_failed" if not ok else "")),
            "message": raw_error or "工具未成功完成",
        }
    summary = compact_text(
        event.get("summary")
        or digest.get("summary")
        or (error or {}).get("message")
        or ("工具调用成功" if ok else "工具调用失败"),
        limit=min(1800, observation_limit),
    )
    data_ref = str(
        event.get("data_ref")
        or digest.get("artifact_id")
        or digest.get("job_id")
        or digest.get("run_id")
        or ""
    )
    return ToolObservation(
        task_id=str(event.get("task_id") or (call.task_id if call else "")),
        tool_id=str(event.get("tool") or (call.tool_id if call else "")),
        status=status,
        ok=ok,
        summary=summary,
        digest=digest,
        data_ref=data_ref,
        degraded_channels=degraded_channels,
        error=error,
        args_hash=str(event.get("args_hash") or (call.args_hash if call else "")),
        writes_selection=bool(
            event.get("writes_selection")
            if "writes_selection" in event
            else (call.writes_selection if call else False)
        ),
        started_at=str(call.started_at if call else event.get("started_at") or ""),
    )
