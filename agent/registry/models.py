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


@dataclass
class SkillSpec:
    skill_id: str
    plugin_id: str
    title: str
    description: str = ""
    tools: list[str] = field(default_factory=list)
    limits: dict[str, Any] = field(default_factory=dict)


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


@dataclass
class ProfileSpec:
    profile_id: str
    display_name: str
    plugins: dict[str, dict[str, Any]] = field(default_factory=dict)
    skills: dict[str, dict[str, Any]] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    budgets: dict[str, Any] = field(default_factory=dict)
    catalog_opt_in_only: bool = True
