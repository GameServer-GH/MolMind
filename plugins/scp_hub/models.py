"""Stable, serialisable models used at the MCP boundary."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any
import hashlib
import json


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


@dataclass
class SCPServerSpec:
    server_id: str
    title: str = ""
    endpoint: str = ""
    transport: str = "streamable_http"
    auth_type: str = "scp_hub_api_key"
    enabled: bool = False
    allowlisted: bool = False
    source: str = "single_server"
    schema_hash: str = ""
    last_discovered_at: str = ""

@dataclass
class SCPSkillSpec:
    skill_id: str
    title: str = ""
    description: str = ""
    server_ids: list[str] = field(default_factory=list)
    tool_ids: list[str] = field(default_factory=list)
    source: dict[str, Any] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=lambda: ["agent_tool"])
    activation: str = "user_opt_in"


@dataclass
class MCPToolDescriptor:
    name: str
    title: str = ""
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    server_id: str = ""
    wire_tool_name: str = ""
    canonical_tool_id: str = ""
    descriptor_hash: str = ""

    def __post_init__(self) -> None:
        self.wire_tool_name = self.wire_tool_name or self.name
        self.canonical_tool_id = self.canonical_tool_id or (f"scp:{self.server_id}:{self.name}" if self.server_id else self.name)
        if not self.descriptor_hash:
            self.descriptor_hash = canonical_hash({"name": self.name, "title": self.title, "description": self.description, "inputSchema": self.input_schema, "outputSchema": self.output_schema, "annotations": self.annotations, "meta": self.meta})

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MCPContentBlock:
    kind: str
    value: Any = None
    mime_type: str = ""
    uri: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = canonical_hash({"kind": self.kind, "value": self.value, "mime_type": self.mime_type, "uri": self.uri})


@dataclass
class SCPObservation:
    source: str = "scp-hub"
    server_id: str = ""
    tool_name: str = ""
    skill_id: str = ""
    status: str = "hit"
    cache_status: str = "unknown"  # live | cache_hit | unknown
    evidence_role: str = "live_supplementary"
    participates_in_ranking: bool = False
    writes_selection: bool = False
    identity: dict[str, Any] = field(default_factory=dict)
    claims: list[Any] = field(default_factory=list)
    citations: list[Any] = field(default_factory=list)
    content: list[MCPContentBlock] = field(default_factory=list)
    request_hash: str = ""
    response_hash: str = ""
    schema_hash: str = ""
    retrieved_at: str = ""
    error_code: str = ""
