"""Session-scoped SCP Skill installation and dynamic Agent Registry binding."""
from __future__ import annotations
from typing import Any
from agent.registry.models import SkillSpec, ToolSpec
from .catalog import SCPCatalog
from .client import MCPClient, MCPError
from .credentials import credential_status, get_api_key
from .cache import SCPQueryCache
from .models import MCPContentBlock, SCPObservation
import time
from datetime import datetime, timezone
from .models import canonical_hash
from .policy import validate_outbound_arguments
from .policy import validate_tool_descriptor

class SCPRegistryManager:
    def __init__(self, registry: Any, catalog: SCPCatalog | None = None, cache: SCPQueryCache | None = None):
        self.registry, self.catalog, self.cache = registry, catalog or SCPCatalog(), cache or SCPQueryCache()

    def install(self, session: Any, skill_id: str, *, client_factory: Any = MCPClient) -> dict[str, Any]:
        item = self.catalog.get(skill_id); key, _ = get_api_key()
        if not item.get("servers"):
            raise MCPError("not_configured", f"SCP skill {skill_id} has no pinned server/tool dependencies in the local catalog")
        if not key: raise MCPError("auth_missing", "SCP_HUB_API_KEY is required to install an SCP skill")
        descriptors: list[tuple[dict[str, Any], Any]] = []
        for server in item.get("servers") or []:
            client = client_factory(server["endpoint"], api_key=key); client.initialize()
            found = {tool.name: tool for tool in client.list_all_tools()}
            required = [str(x) for x in server.get("tools") or []]
            missing = [name for name in required if name not in found]
            if missing: raise MCPError("skill_tool_mismatch", f"Skill {skill_id} references missing tools: {', '.join(missing)}")
            for name in required:
                descriptor = found[name]
                validate_tool_descriptor({"name": descriptor.name, "annotations": descriptor.annotations})
                descriptors.append((server, descriptor))
        tool_ids: list[str] = []
        for server, desc in descriptors:
            tool_id = f"scp:{server['server_id']}:{desc.name}"; tool_ids.append(tool_id)
            self.registry.register_dynamic_tool(ToolSpec(tool_id=tool_id, plugin_id="scp-hub", title=desc.title or desc.name, description=desc.description, risk="R0", writes_selection=False, input_schema=desc.input_schema, output_schema=desc.output_schema, annotations=desc.annotations, meta=desc.meta, wire_tool_name=desc.wire_tool_name, server_id=server["server_id"], descriptor_hash=desc.descriptor_hash, timeout_sec=float(item.get("timeout_sec") or 30), dynamic=True))
        plugin = self.registry.plugins.get("scp-hub")
        capabilities = list(getattr(plugin, "capabilities", []) or []) if plugin else []
        capability_ids = [
            str(cap.get("capability_id"))
            for cap in capabilities
            if isinstance(cap, dict) and str(cap.get("skill_id") or "") == skill_id
        ]
        capability_lock_hash = canonical_hash({
            "capabilities": [cap for cap in capabilities if str(cap.get("skill_id") or "") == skill_id],
            "terminology": getattr(plugin, "terminology", {}) if plugin else {},
        })
        self.registry.register_dynamic_skill(SkillSpec(skill_id=skill_id, plugin_id="scp-hub", title=str(item.get("title") or skill_id), description=str(item.get("description") or ""), tools=tool_ids, limits={"max_calls_per_turn": 4}, capability_ids=capability_ids))
        state = {"skill_id": skill_id, "enabled": True, "installed_at": datetime.now(timezone.utc).isoformat(), "catalog_lock_hash": canonical_hash(item), "capability_lock_hash": capability_lock_hash, "capability_ids": capability_ids, "credential_status": credential_status(key, authorized=True), "servers": item.get("servers") or [], "tools": tool_ids, "tool_descriptors": [self.registry.tools[tid].__dict__ for tid in tool_ids], "writes_selection": False, "participates_in_ranking": False}
        session.installed_scp_skills[skill_id] = state
        if "scp-hub" not in session.installed_catalog: session.installed_catalog.append("scp-hub")
        return state

    @staticmethod
    def _synthesize_tool(tool_id: str) -> ToolSpec | None:
        """Build a minimal ToolSpec from a locked `scp:{server}:{wire}` id."""
        parts = str(tool_id).split(":", 2)
        if len(parts) != 3 or parts[0] != "scp" or not parts[1] or not parts[2]:
            return None
        server_id, wire_name = parts[1], parts[2]
        return ToolSpec(
            tool_id=tool_id,
            plugin_id="scp-hub",
            title=wire_name,
            wire_tool_name=wire_name,
            server_id=server_id,
            dynamic=True,
            writes_selection=False,
        )

    def restore_session(self, session: Any) -> None:
        """Rehydrate locked dynamic descriptors after a process restart."""
        for skill_id, state in (getattr(session, "installed_scp_skills", {}) or {}).items():
            if not isinstance(state, dict):
                continue
            descriptors = state.get("tool_descriptors") or []
            for raw in descriptors:
                if not isinstance(raw, dict):
                    continue
                tool_id = str(raw.get("tool_id") or "")
                if not tool_id or tool_id in self.registry.tools:
                    continue
                try:
                    self.registry.register_dynamic_tool(
                        ToolSpec(
                            **{
                                key: value
                                for key, value in raw.items()
                                if key in ToolSpec.__dataclass_fields__
                            }
                        )
                    )
                except (TypeError, ValueError):
                    continue
            tool_ids = [str(tid) for tid in (state.get("tools") or []) if str(tid)]
            for tool_id in tool_ids:
                if tool_id in self.registry.tools:
                    continue
                synthesized = self._synthesize_tool(tool_id)
                if synthesized is None:
                    continue
                try:
                    self.registry.register_dynamic_tool(synthesized)
                except ValueError:
                    continue
            if skill_id in self.registry.skills:
                continue
            missing = [tid for tid in tool_ids if tid not in self.registry.tools]
            if missing:
                # Incomplete lock (legacy session without descriptors) — skip
                # rather than crashing process startup / session load.
                continue
            self.registry.register_dynamic_skill(
                SkillSpec(
                    skill_id=skill_id,
                    plugin_id="scp-hub",
                    title=str(state.get("title") or skill_id),
                    tools=tool_ids,
                    capability_ids=list(state.get("capability_ids") or []),
                )
            )

    def set_enabled(self, session: Any, skill_id: str, enabled: bool) -> dict[str, Any]:
        if skill_id not in session.installed_scp_skills: raise KeyError(f"SCP skill is not installed: {skill_id}")
        session.installed_scp_skills[skill_id]["enabled"] = bool(enabled)
        return session.installed_scp_skills[skill_id]

    def uninstall(self, session: Any, skill_id: str) -> None:
        if skill_id in session.installed_scp_skills:
            session.installed_scp_skills.pop(skill_id); self.registry.unregister_dynamic_skill(skill_id)

    def call(self, session: Any, tool_id: str, arguments: dict[str, Any], *, allow_live: bool, force_refresh: bool = False, stage: bool = False, molecule_id: str = "") -> Any:
        if not allow_live: raise PermissionError("allow_live=true is required for MCP calls")
        validate_outbound_arguments(arguments)
        tool = self.registry.tools.get(tool_id)
        if not tool or tool.plugin_id != "scp-hub" or tool.writes_selection: raise PermissionError("SCP tool is unavailable")
        skill_id = next((sid for sid, state in session.installed_scp_skills.items() if state.get("enabled") and tool_id in state.get("tools", [])), "")
        if not skill_id: raise PermissionError("SCP skill is disabled or not installed")
        state = session.installed_scp_skills[skill_id]; server = next((x for x in state.get("servers", []) if x.get("server_id") == tool.server_id), None)
        if not server: raise RuntimeError("SCP server metadata missing")
        key, _ = get_api_key(); wire_name = tool.wire_tool_name or tool_id.rsplit(":", 1)[-1]
        cache_key = self.cache.key(server_id=tool.server_id, tool_name=wire_name, schema_hash=tool.descriptor_hash, arguments=arguments, scope=session.session_id)
        cached = self.cache.get(cache_key, force_refresh=force_refresh)
        if cached:
            raw = dict(cached.get("observation") or {}); raw["content"] = [MCPContentBlock(**item) for item in raw.get("content") or []]; raw["cache_status"] = "cache_hit"
            self.cache.record_call({"session_id": session.session_id, "skill_id": skill_id, "server_id": tool.server_id, "tool_name": wire_name, "schema_hash": tool.descriptor_hash, "request_hash": cache_key, "response_hash": raw.get("response_hash", ""), "status": raw.get("status", "hit"), "cache_status": "cache_hit", "duration_ms": 0})
            return SCPObservation(**raw)
        started = time.monotonic(); client = MCPClient(server["endpoint"], api_key=key, timeout=tool.timeout_sec or 30); client.initialize()
        observation = client.call_tool(wire_name, arguments, skill_id=skill_id, server_id=tool.server_id)
        self.cache.put(cache_key, observation)
        if stage: self.cache.stage(observation, session_id=session.session_id, molecule_id=molecule_id, reason="explicit_mcp_capture")
        self.cache.record_call({"session_id": session.session_id, "skill_id": skill_id, "server_id": tool.server_id, "tool_name": wire_name, "schema_hash": tool.descriptor_hash, "request_hash": observation.request_hash, "response_hash": observation.response_hash, "status": observation.status, "cache_status": observation.cache_status, "duration_ms": int((time.monotonic()-started)*1000)})
        return observation
