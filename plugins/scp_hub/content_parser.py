"""Parse MCP result content without treating remote paths as local files."""
from __future__ import annotations
import json
from typing import Any
from .models import MCPContentBlock, canonical_hash

def parse_content(result: Any) -> tuple[list[MCPContentBlock], Any]:
    structured = result.get("structuredContent") if isinstance(result, dict) else getattr(result, "structuredContent", None)
    raw = result.get("content", []) if isinstance(result, dict) else getattr(result, "content", [])
    blocks: list[MCPContentBlock] = []
    for item in raw or []:
        item = item if isinstance(item, dict) else {k: getattr(item, k, None) for k in ("type", "text", "data", "mimeType", "uri", "name")}
        kind = str(item.get("type") or "text")
        if kind == "text":
            value: Any = item.get("text", "")
            try: value = json.loads(value) if isinstance(value, str) else value
            except (TypeError, json.JSONDecodeError): pass
        elif kind in {"image", "audio"}: value = item.get("data")
        elif kind in {"resource", "resource_link"}: value = {"uri": str(item.get("uri") or (item.get("resource") or {}).get("uri") or ""), "name": item.get("name")}
        else: value = item
        blocks.append(MCPContentBlock(kind=kind, value=value, mime_type=str(item.get("mimeType") or ""), uri=str(item.get("uri") or "")))
    return blocks, structured

def response_hash(result: Any) -> str:
    return canonical_hash(result)
