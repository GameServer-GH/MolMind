"""Small audited MCP Streamable HTTP compatibility client.

It intentionally speaks only JSON-RPC over the allow-listed HTTP endpoint.  A
future Python 3.10+ deployment can swap this transport for the official SDK
without changing descriptors or observations.
"""
from __future__ import annotations
import json
import time
import uuid
from enum import Enum
from typing import Any, Callable
import httpx
from .content_parser import parse_content, response_hash
from .credentials import get_api_key
from .models import MCPToolDescriptor, SCPObservation, canonical_hash
from .policy import DEFAULT_POLICY, validate_endpoint, validate_tool_descriptor

class MCPErrorCode(str, Enum):
    NOT_CONFIGURED="not_configured"; AUTH_MISSING="auth_missing"; AUTH_INVALID="auth_invalid"; SERVER_UNREACHABLE="server_unreachable"; TIMEOUT="timeout"; RATE_LIMITED="rate_limited"; SCHEMA_CHANGED="schema_changed"; INVALID_ARGUMENTS="invalid_arguments"; TOOL_NOT_FOUND="tool_not_found"; TOOL_FAILED="tool_failed"; INVALID_RESPONSE="invalid_response"; CANCELLED="cancelled"; REMOTE_ARTIFACT_UNAVAILABLE="remote_artifact_unavailable"

class MCPError(RuntimeError):
    def __init__(self, code: MCPErrorCode | str, message: str, *, status_code: int | None = None):
        self.code = code.value if isinstance(code, MCPErrorCode) else str(code)
        self.status_code = status_code
        super().__init__(message)

class MCPClient:
    def __init__(self, endpoint: str, *, api_key: str | None = None, timeout: float = 30.0, max_response_bytes: int = 2_000_000, transport: httpx.BaseTransport | None = None, audit: Callable[[dict[str, Any]], None] | None = None):
        self.endpoint = validate_endpoint(endpoint)
        self.api_key = api_key if api_key is not None else get_api_key()[0]
        self.timeout, self.max_response_bytes, self.audit = timeout, max_response_bytes, audit
        self.session_id: str | None = None
        self.protocol_version = "2025-06-18"
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self._id = 0
        self._transport = transport

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.api_key: raise MCPError(MCPErrorCode.AUTH_MISSING, "SCP Hub API key is not configured")
        self._id += 1; request_id = self._id; started = time.monotonic()
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "SCP-HUB-API-KEY": self.api_key}
        if self.session_id: headers["Mcp-Session-Id"] = self.session_id
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        try:
            with httpx.Client(timeout=self.timeout, transport=self._transport, follow_redirects=False) as client:
                response = client.post(self.endpoint, headers=headers, json=body)
        except httpx.TimeoutException as exc: raise MCPError(MCPErrorCode.TIMEOUT, "MCP request timed out") from exc
        except httpx.HTTPError as exc: raise MCPError(MCPErrorCode.SERVER_UNREACHABLE, "MCP server unreachable") from exc
        if len(response.content) > self.max_response_bytes: raise MCPError(MCPErrorCode.INVALID_RESPONSE, "MCP response exceeds configured size limit", status_code=response.status_code)
        if response.status_code in (401, 403): raise MCPError(MCPErrorCode.AUTH_INVALID, "SCP Hub API key rejected", status_code=response.status_code)
        if response.status_code == 429: raise MCPError(MCPErrorCode.RATE_LIMITED, "MCP server rate limited the request", status_code=429)
        if response.status_code >= 500: raise MCPError(MCPErrorCode.SERVER_UNREACHABLE, "MCP server returned a server error", status_code=response.status_code)
        try:
            if "text/event-stream" in response.headers.get("content-type", ""):
                events = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
                payload = json.loads(events[-1]) if events else None
            else: payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc: raise MCPError(MCPErrorCode.INVALID_RESPONSE, "MCP response is not JSON", status_code=response.status_code) from exc
        if response.headers.get("Mcp-Session-Id"): self.session_id = response.headers["Mcp-Session-Id"]
        if isinstance(payload, dict) and payload.get("error"):
            err = payload["error"]; msg = str(err.get("message") or "MCP JSON-RPC error")
            code = MCPErrorCode.TOOL_NOT_FOUND if method == "tools/call" and "not found" in msg.lower() else MCPErrorCode.TOOL_FAILED
            raise MCPError(code, msg, status_code=response.status_code)
        if not isinstance(payload, dict) or "result" not in payload: raise MCPError(MCPErrorCode.INVALID_RESPONSE, "MCP result missing")
        if self.audit: self.audit({"method": method, "request_hash": canonical_hash(body), "response_hash": response_hash(payload), "duration_ms": int((time.monotonic()-started)*1000), "status": "ok"})
        return payload["result"]

    def initialize(self) -> dict[str, Any]:
        result = self._request("initialize", {"protocolVersion": self.protocol_version, "capabilities": {}, "clientInfo": {"name": "molmind", "version": "0.2.3"}})
        self.protocol_version = str(result.get("protocolVersion") or self.protocol_version); self.server_info = dict(result.get("serverInfo") or {}); self.capabilities = dict(result.get("capabilities") or {})
        self._notify("notifications/initialized", {})
        return result

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "SCP-HUB-API-KEY": self.api_key}
        if self.session_id: headers["Mcp-Session-Id"] = self.session_id
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            with httpx.Client(timeout=self.timeout, transport=self._transport, follow_redirects=False) as client:
                response = client.post(self.endpoint, headers=headers, json=body)
            if response.status_code >= 400: raise MCPError(MCPErrorCode.SERVER_UNREACHABLE, "MCP notification rejected", status_code=response.status_code)
        except httpx.HTTPError as exc: raise MCPError(MCPErrorCode.SERVER_UNREACHABLE, "MCP notification failed") from exc

    def ping(self) -> Any: return self._request("ping", {})

    send_ping = ping

    def _list_protocol_items(self, method: str, key: str, *, cursor: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        result = self._request(method, {"cursor": cursor} if cursor else {})
        items = result.get(key) or []
        if not isinstance(items, list):
            raise MCPError(MCPErrorCode.INVALID_RESPONSE, f"MCP {method} returned invalid {key}")
        return [dict(item) for item in items if isinstance(item, dict)], result.get("nextCursor")

    def list_resources(self, *, cursor: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        return self._list_protocol_items("resources/list", "resources", cursor=cursor)

    def list_resource_templates(self, *, cursor: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        return self._list_protocol_items("resources/templates/list", "resourceTemplates", cursor=cursor)

    def list_prompts(self, *, cursor: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        return self._list_protocol_items("prompts/list", "prompts", cursor=cursor)

    def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("prompts/get", {"name": name, "arguments": arguments or {}})

    def list_tools(self, *, cursor: str | None = None) -> tuple[list[MCPToolDescriptor], str | None]:
        result = self._request("tools/list", {"cursor": cursor} if cursor else {})
        tools: list[MCPToolDescriptor] = []
        for raw in result.get("tools") or []:
            # Discovery must not fail merely because the same server exposes
            # another high-risk tool. Registration validates each selected
            # descriptor and keeps execution/file/device tools unavailable.
            tools.append(MCPToolDescriptor(name=str(raw["name"]), title=str(raw.get("title") or ""), description=str(raw.get("description") or ""), input_schema=dict(raw.get("inputSchema") or {}), output_schema=raw.get("outputSchema"), annotations=dict(raw.get("annotations") or {}), meta=dict(raw.get("_meta") or raw.get("meta") or {})))
        return tools, result.get("nextCursor")

    def list_all_tools(self) -> list[MCPToolDescriptor]:
        out, cursor = [], None
        while True:
            page, cursor = self.list_tools(cursor=cursor); out.extend(page)
            if not cursor: return out

    def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None, *, skill_id: str = "", server_id: str = "") -> SCPObservation:
        args = arguments or {}; started = time.time()
        result = self._request("tools/call", {"name": tool_name, "arguments": args})
        blocks, structured = parse_content(result)
        obs = SCPObservation(server_id=server_id, tool_name=tool_name, skill_id=skill_id, status="failed" if result.get("isError") else "hit", cache_status="live", content=blocks, request_hash=canonical_hash(args), response_hash=response_hash(result), retrieved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)))
        if result.get("isError"): obs.error_code = MCPErrorCode.TOOL_FAILED.value
        if structured is not None: obs.claims = [structured]
        return obs
