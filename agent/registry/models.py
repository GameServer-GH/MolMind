"""Registry 数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolSpec:
    tool_id: str
    plugin_id: str
    title: str
    risk: str = "R0"  # R0 | R1 | R2
    description: str = ""
    writes_selection: bool = False
    limits: dict[str, Any] = field(default_factory=dict)
    #: Declarative execution contract. The planner may select only tools whose
    #: preconditions are satisfied; the runtime validates arguments again.
    input_schema: dict[str, Any] = field(default_factory=dict)
    requires: list[str] = field(default_factory=list)
    produces: list[str] = field(default_factory=list)
    idempotent: bool = False
    timeout_sec: float | None = None
    confirmation_required: bool = False
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    wire_tool_name: str = ""
    server_id: str = ""
    descriptor_hash: str = ""
    dynamic: bool = False


@dataclass
class SkillSpec:
    skill_id: str
    plugin_id: str
    title: str
    description: str = ""
    tools: list[str] = field(default_factory=list)
    limits: dict[str, Any] = field(default_factory=dict)
    requires: list[str] = field(default_factory=list)
    produces: list[str] = field(default_factory=list)
    capability_ids: list[str] = field(default_factory=list)


@dataclass
class PluginSpec:
    plugin_id: str
    title: str
    builtin: bool = False
    enabled: bool = False
    catalog: bool = False
    description: str = ""
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    source: str = ""
    requires: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    tool_limits: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Plugin-owned network defaults.  This keeps live access policy attached
    #: to the capability bundle instead of scattering it through chat intent.
    network_policy: dict[str, Any] = field(default_factory=dict)
    #: Declarative capabilities consumed by the Task Router. Kept as plain
    #: mappings in Phase 1 so plugins can evolve without a code release.
    capabilities: list[dict[str, Any]] = field(default_factory=list)
    #: Canonical domain vocabulary and aliases owned by the plugin.
    terminology: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProfileSpec:
    profile_id: str
    display_name: str
    plugins: dict[str, dict[str, Any]] = field(default_factory=dict)
    skills: dict[str, dict[str, Any]] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    budgets: dict[str, Any] = field(default_factory=dict)
    catalog_opt_in_only: bool = True
