"""Conservative network and tool policy for remote SCP capabilities."""
from __future__ import annotations
from urllib.parse import urlparse
from typing import Any
import json, re

DEFAULT_POLICY = {"allow_live_default": False, "max_calls_per_turn": 4, "max_servers_per_turn": 2, "max_response_bytes": 2_000_000, "default_timeout_sec": 30.0, "max_interactive_timeout_sec": 120.0, "writes_selection": False}

def validate_endpoint(endpoint: str, allowlist: tuple[str, ...] = ("scphub.intern-ai.org.cn", "scp.intern-ai.org.cn")) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in {x.lower() for x in allowlist}:
        raise ValueError("MCP endpoint must be HTTPS and belong to the SCP Hub allowlist")
    return endpoint.rstrip("/")

def validate_tool_descriptor(descriptor: dict[str, Any]) -> None:
    name = str(descriptor.get("name") or "")
    if not name or len(name) > 200:
        raise ValueError("invalid MCP tool name")
    ann = descriptor.get("annotations") or {}
    if bool(ann.get("destructive") or ann.get("openWorld")) or name.startswith(("execute_", "shell", "code_")):
        raise PermissionError("high-risk MCP tool is disabled by policy")

def validate_outbound_arguments(arguments: dict[str, Any], *, max_bytes: int = 200_000) -> None:
    if not isinstance(arguments, dict): raise ValueError("MCP arguments must be an object")
    secret = re.compile(r"(?:api.?key|authorization|password|secret|token)", re.I)
    def walk(value: Any, key: str = "") -> None:
        if secret.search(key): raise PermissionError("credentials may not be sent as MCP tool arguments")
        if isinstance(value, dict):
            for child_key, child in value.items(): walk(child, str(child_key))
        elif isinstance(value, list):
            for child in value: walk(child, key)
        elif isinstance(value, (bytes, bytearray)): raise PermissionError("binary/file payloads require a separate approved transfer flow")
    walk(arguments)
    if len(json.dumps(arguments, ensure_ascii=False, default=str).encode()) > max_bytes: raise ValueError("MCP arguments exceed the outbound data limit")
