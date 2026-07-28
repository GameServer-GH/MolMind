"""Catalog 插件公共约定：enrichment 信封、永不写主榜。"""

from __future__ import annotations

from typing import Any


def enrichment_envelope(
    *,
    tool: str,
    plugin: str,
    ok: bool = True,
    data: Any = None,
    message: str = "",
    degraded: list[str] | None = None,
    citations: list[dict[str, Any]] | None = None,
    digest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """统一 Tool 返回信封。`writes_selection` 恒为 False。"""
    return {
        "ok": ok,
        "tool": tool,
        "plugin": plugin,
        "writes_selection": False,
        "data": data,
        "message": message,
        "degraded": list(degraded or []),
        "citations": list(citations or []),
        "digest": dict(digest or {}),
        "claim_ceiling": "enrichment_only",
    }


def assert_no_selection_write(result: dict[str, Any]) -> None:
    if result.get("writes_selection"):
        raise AssertionError(
            f"Catalog tool {result.get('tool')} 试图写主榜，已拒绝"
        )
