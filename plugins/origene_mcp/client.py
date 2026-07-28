"""OrigeneMCP HTTP/MCP 客户端骨架。

默认不发网络请求。配置 ``MOLMIND_ORIGENE_MCP_URL`` 后才尝试探测；
失败一律降级，不影响 molmind-core 主榜。
"""

from __future__ import annotations

import os
from typing import Any

import httpx


class OrigeneMcpClient:
    """轻量探测/查询客户端。赛期默认 stub。"""

    def __init__(self, base_url: str | None = None, *, timeout_s: float = 8.0) -> None:
        self.base_url = (base_url or os.environ.get("MOLMIND_ORIGENE_MCP_URL") or "").rstrip(
            "/"
        )
        self.timeout_s = timeout_s

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def health(self) -> dict[str, Any]:
        if not self.configured:
            return {"ok": False, "configured": False, "detail": "MOLMIND_ORIGENE_MCP_URL 未设置"}
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                resp = client.get(f"{self.base_url}/health")
            return {
                "ok": resp.status_code < 400,
                "configured": True,
                "status_code": resp.status_code,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "configured": True, "detail": str(exc)}

    def query(self, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """调用远端 MCP 工具。未配置或失败时返回 ok=False（由上层降级）。"""
        if not self.configured:
            return {"ok": False, "degraded": "origene_mcp_not_configured"}
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                resp = client.post(
                    f"{self.base_url}/tools/{tool_name}",
                    json=payload,
                )
            if resp.status_code >= 400:
                return {
                    "ok": False,
                    "degraded": "origene_mcp_http_error",
                    "status_code": resp.status_code,
                }
            data = resp.json() if resp.content else {}
            return {"ok": True, "data": data}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "degraded": "origene_mcp_unreachable", "detail": str(exc)}
