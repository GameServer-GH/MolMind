"""Two-level SCP discovery: aggregate indexing, single-server verification."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
import json
from .client import MCPClient
from .models import MCPToolDescriptor, SCPServerSpec, canonical_hash

def discover_server(server: SCPServerSpec, *, api_key: str, client_factory: Callable[..., MCPClient] = MCPClient) -> dict[str, Any]:
    client = client_factory(server.endpoint, api_key=api_key)
    client.initialize()
    tools = client.list_all_tools()
    now = datetime.now(timezone.utc).isoformat()
    return {"server": {**server.__dict__, "enabled": True, "allowlisted": True, "schema_hash": canonical_hash([tool.as_dict() for tool in tools]), "last_discovered_at": now}, "tools": [tool.as_dict() for tool in tools]}

def merge_discovery(index_path: Path, discovered: dict[str, Any]) -> dict[str, Any]:
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {"version": 1, "skills": [], "servers": [], "tools": []}
    index["servers"] = [item for item in index.get("servers", []) if item.get("server_id") != discovered["server"].get("server_id")]
    index["servers"].append(discovered["server"])
    existing = [item for item in index.get("tools", []) if item.get("server_id") != discovered["server"].get("server_id")]
    for tool in discovered.get("tools", []):
        tool["server_id"] = discovered["server"].get("server_id", "")
        tool["canonical_tool_id"] = f"scp:{tool['server_id']}:{tool['name']}"
        existing.append(tool)
    index["tools"] = existing
    index_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = index_path.with_suffix(index_path.suffix + ".tmp"); tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"); tmp.replace(index_path)
    return index
