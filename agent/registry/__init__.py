"""加载 configs/agent 下的 Profile / Plugin / Skill / Catalog。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from agent.registry.models import PluginSpec, ProfileSpec, SkillSpec, ToolSpec

ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = ROOT / "configs" / "agent"

# Per-tool display copy (YAML only lists tool_id; keep UI intros unique).
TOOL_META: dict[str, dict[str, str]] = {
    "parse_sdf": {
        "title": "解析 SDF",
        "description": "读取并解析上传的 SDF 分子库，建立后续打分可用的结构输入。",
    },
    "score_and_rank": {
        "title": "打分排序",
        "description": "硬过滤后做降脂/毒性打分与多样性排序，写出主排序结果（唯一写榜入口）。",
    },
    "export_nomination": {
        "title": "导出候选清单",
        "description": (
            "将当前冻结 TopN 导出为提名 CSV；列集合与顺序锁定（schema_locked），"
            "不可按用户口头指定附加列；旁证 enrich 不改此 CSV schema。"
        ),
    },
    "query_evidence": {
        "title": "查询证据",
        "description": "按分子拉取公开/本地证据快照，填充证据卡片所需字段。",
    },
    "start_mechanism_report": {
        "title": "启动机制报告",
        "description": "异步启动机制假说 PDF 生成任务，返回 job id 供轮询。",
    },
    "get_mechanism_job": {
        "title": "查询机制任务",
        "description": "查询机制报告任务状态与产物路径。",
    },
    "get_run_audit": {
        "title": "运行审计",
        "description": "汇总本次筛选的配置、门槛与审计信息，便于复现与核对。",
    },
    "build_evidence_card": {
        "title": "构建证据卡",
        "description": "为 TopN 分子组装结构化证据卡片（机制、毒性、引用等）。",
    },
    "export_submission_bundle": {
        "title": "导出结果包",
        "description": "打包候选清单、冻结候补 CSV、血缘清单、轨迹与可用机制报告，生成可复现的结果归档包。",
    },
    "draft_nomination_review": {
        "title": "起草复核建议",
        "description": "基于证据与排名草稿生成人工复核建议（不直接改榜）。",
    },
    "apply_review": {
        "title": "应用复核调整",
        "description": "在人工确认后应用复核调整；高风险写操作。",
    },
    "eval_goldset": {
        "title": "金标评估",
        "description": "用金标集评估当前打分/排序表现，输出对照指标。",
    },
    "mcp_query_opentargets": {
        "title": "OpenTargets 查询",
        "description": "经 OrigeneMCP 查询 OpenTargets，补充靶点/适应症旁证。",
    },
    "mcp_query_chembl": {
        "title": "ChEMBL 查询",
        "description": "经 OrigeneMCP 查询 ChEMBL，补充活性与化学信息旁证。",
    },
    "mcp_query_uniprot": {
        "title": "UniProt 查询",
        "description": "经 OrigeneMCP 查询 UniProt，补充蛋白序列与功能注释。",
    },
    "predict_pl_fitness": {
        "title": "蛋白–配体 fitness",
        "description": "对 TopN 分子预测蛋白–配体 fitness，结果仅作旁证写入证据卡/PDF。",
    },
}


def _tool_meta(tool_id: str) -> tuple[str, str]:
    meta = TOOL_META.get(tool_id) or {}
    title = str(meta.get("title") or tool_id.replace("_", " "))
    description = str(meta.get("description") or "")
    return title, description


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML 根节点须为 mapping: {path}")
    return data


def _load_plugin_file(path: Path, *, catalog: bool) -> PluginSpec:
    raw = _read_yaml(path)
    tool_limits_raw = raw.get("tool_limits") or {}
    tool_limits: dict[str, dict[str, Any]] = {}
    if isinstance(tool_limits_raw, dict):
        for tid, lim in tool_limits_raw.items():
            if isinstance(lim, dict):
                tool_limits[str(tid)] = dict(lim)
    return PluginSpec(
        plugin_id=str(raw.get("plugin_id") or path.stem),
        title=str(raw.get("title") or path.stem),
        builtin=bool(raw.get("builtin", False)),
        enabled=bool(raw.get("enabled", False)),
        catalog=catalog or bool(raw.get("catalog", False)),
        description=str(raw.get("description") or ""),
        tools=list(raw.get("tools") or []),
        skills=list(raw.get("skills") or []),
        source=str(raw.get("source") or ""),
        requires=dict(raw.get("requires") or {}),
        limits=dict(raw.get("limits") or {}),
        tool_limits=tool_limits,
        network_policy=dict(raw.get("network_policy") or {}),
        capabilities=[item for item in (raw.get("capabilities") or []) if isinstance(item, dict)],
        terminology=dict(raw.get("terminology") or {}),
    )


class AgentRegistry:
    """进程内 Registry；赛期单进程足够。"""

    def __init__(self, config_root: Path | None = None) -> None:
        self.config_root = config_root or CONFIG_ROOT
        self.profiles: dict[str, ProfileSpec] = {}
        self.plugins: dict[str, PluginSpec] = {}
        self.skills: dict[str, SkillSpec] = {}
        self.tools: dict[str, ToolSpec] = {}
        self.reload()

    def reload(self) -> None:
        self.profiles.clear()
        self.plugins.clear()
        self.skills.clear()
        self.tools.clear()

        profiles_dir = self.config_root / "profiles"
        if profiles_dir.is_dir():
            for path in sorted(profiles_dir.glob("*.yaml")):
                raw = _read_yaml(path)
                pid = str(raw.get("profile_id") or path.stem)
                policy = dict(raw.get("policy") or {})
                self.profiles[pid] = ProfileSpec(
                    profile_id=pid,
                    display_name=str(raw.get("display_name") or pid),
                    plugins=dict(raw.get("plugins") or {}),
                    skills=dict(raw.get("skills") or {}),
                    policy=policy,
                    budgets=dict(raw.get("budgets") or {}),
                    catalog_opt_in_only=bool(policy.get("catalog_opt_in_only", True)),
                )

        plugins_dir = self.config_root / "plugins"
        core = plugins_dir / "molmind-core.yaml"
        if core.is_file():
            spec = _load_plugin_file(core, catalog=False)
            self.plugins[spec.plugin_id] = spec
        catalog_dir = plugins_dir / "catalog"
        if catalog_dir.is_dir():
            for path in sorted(catalog_dir.glob("*.yaml")):
                spec = _load_plugin_file(path, catalog=True)
                # Catalog 强制非内置、默认不启用（文件里 enabled 仅作文档；运行时以会话 opt-in 为准）
                spec.builtin = False
                if "enabled" not in _read_yaml(path):
                    spec.enabled = False
                self.plugins[spec.plugin_id] = spec

        skills_dir = self.config_root / "skills"
        if skills_dir.is_dir():
            for path in sorted(skills_dir.glob("*.yaml")):
                raw = _read_yaml(path)
                sid = str(raw.get("skill_id") or path.stem)
                self.skills[sid] = SkillSpec(
                    skill_id=sid,
                    plugin_id=str(raw.get("plugin_id") or "molmind-core"),
                    title=str(raw.get("title") or sid),
                    description=str(raw.get("description") or ""),
                    tools=list(raw.get("tools") or []),
                    limits=dict(raw.get("limits") or {}),
                    requires=[str(x) for x in raw.get("requires") or []],
                    produces=[str(x) for x in raw.get("produces") or []],
                )

        # Tools 从已加载 plugin 声明展开
        # 硬规则：仅 molmind-core.score_and_rank 可写主榜；Catalog 一律 enrichment。
        for plugin in self.plugins.values():
            raw_plugin = _read_yaml(
                (plugins_dir / f"{plugin.plugin_id}.yaml")
                if plugin.plugin_id == "molmind-core"
                else (catalog_dir / f"{plugin.plugin_id}.yaml")
            )
            contracts = raw_plugin.get("tool_contracts") or {}
            for tool_id in plugin.tools:
                writes = (
                    tool_id == "score_and_rank"
                    and plugin.plugin_id == "molmind-core"
                    and not plugin.catalog
                )
                risk = "R1" if writes else "R0"
                if tool_id in ("apply_review",):
                    risk = "R2"
                title, description = _tool_meta(tool_id)
                limits = dict(plugin.tool_limits.get(str(tool_id)) or {})
                contract = dict(contracts.get(str(tool_id)) or {})
                if not limits and plugin.limits:
                    # 工具未单独声明时继承插件级 limits（仅含 top_n_*）
                    limits = {
                        k: v
                        for k, v in plugin.limits.items()
                        if str(k).startswith("top_n_")
                    }
                self.tools[tool_id] = ToolSpec(
                    tool_id=tool_id,
                    plugin_id=plugin.plugin_id,
                    title=title,
                    risk=risk,
                    description=description,
                    writes_selection=writes,
                    limits=limits,
                    input_schema=dict(contract.get("input_schema") or {}),
                    requires=[str(x) for x in contract.get("requires") or []],
                    produces=[str(x) for x in contract.get("produces") or []],
                    idempotent=bool(contract.get("idempotent", False)),
                    timeout_sec=(
                        float(contract["timeout_sec"])
                        if contract.get("timeout_sec") is not None
                        else None
                    ),
                    confirmation_required=bool(
                        contract.get("confirmation_required", risk == "R2")
                    ),
                )

    def get_profile(self, profile_id: str = "competition_masld") -> ProfileSpec:
        if profile_id not in self.profiles:
            raise KeyError(f"未知 Profile: {profile_id}")
        return self.profiles[profile_id]

    def register_dynamic_tool(self, tool: ToolSpec) -> ToolSpec:
        """Register an audited remote descriptor without mutating YAML files."""
        if not tool.tool_id.startswith("scp:") or tool.plugin_id != "scp-hub":
            raise ValueError("dynamic tools must use the scp namespace")
        if tool.plugin_id not in self.plugins:
            raise ValueError(f"tool owner plugin is not registered: {tool.plugin_id}")
        tool.writes_selection = False
        tool.dynamic = True
        self.tools[tool.tool_id] = tool
        return tool

    def register_dynamic_skill(self, skill: SkillSpec) -> SkillSpec:
        if skill.plugin_id != "scp-hub":
            raise ValueError("dynamic SCP skills must belong to scp-hub")
        missing = [tool_id for tool_id in skill.tools if tool_id not in self.tools]
        if missing:
            raise ValueError(f"skill references unregistered tools: {', '.join(missing)}")
        self.skills[skill.skill_id] = skill
        return skill

    def unregister_dynamic_skill(self, skill_id: str) -> None:
        skill = self.skills.get(skill_id)
        if not skill or skill.plugin_id != "scp-hub": return
        self.skills.pop(skill_id, None)
        referenced = {tid for item in self.skills.values() for tid in item.tools}
        for tool_id in skill.tools:
            tool = self.tools.get(tool_id)
            if tool and tool.dynamic and tool_id not in referenced:
                self.tools.pop(tool_id, None)

    def list_catalog(self) -> list[PluginSpec]:
        return [p for p in self.plugins.values() if p.catalog]

    def list_builtin(self) -> list[PluginSpec]:
        return [p for p in self.plugins.values() if p.builtin]

    def resolve_top_n_bounds(
        self,
        *,
        skill_ids: list[str] | tuple[str, ...] | None = None,
        tool_ids: list[str] | tuple[str, ...] | None = None,
        plugin_ids: list[str] | tuple[str, ...] | None = None,
    ) -> tuple[int, int]:
        """Resolve top_n min/max from involved skill / tool / plugin limits.

        Takes the tightest intersection (max of mins, min of maxes). Falls back to
        scientific pipeline constants when nothing declares limits.
        """
        from plugins.molmind_core.scientific.pipeline.runner import TOP_N_MAX, TOP_N_MIN

        mins: list[int] = []
        maxs: list[int] = []

        def _absorb(limits: dict[str, Any] | None) -> None:
            if not limits:
                return
            if "top_n_min" in limits:
                try:
                    mins.append(int(limits["top_n_min"]))
                except (TypeError, ValueError):
                    pass
            if "top_n_max" in limits:
                try:
                    maxs.append(int(limits["top_n_max"]))
                except (TypeError, ValueError):
                    pass

        for sid in skill_ids or ():
            skill = self.skills.get(str(sid))
            if skill:
                _absorb(skill.limits)
                for tid in skill.tools:
                    tool = self.tools.get(tid)
                    if tool:
                        _absorb(tool.limits)
                plugin = self.plugins.get(skill.plugin_id)
                if plugin:
                    _absorb(plugin.limits)

        for tid in tool_ids or ():
            tool = self.tools.get(str(tid))
            if tool:
                _absorb(tool.limits)
                plugin = self.plugins.get(tool.plugin_id)
                if plugin:
                    _absorb(plugin.limits)

        for pid in plugin_ids or ():
            plugin = self.plugins.get(str(pid))
            if plugin:
                _absorb(plugin.limits)

        if not mins and not maxs:
            # Default nomination path
            skill = self.skills.get("masld_nominate")
            if skill:
                _absorb(skill.limits)
            tool = self.tools.get("score_and_rank")
            if tool:
                _absorb(tool.limits)
            plugin = self.plugins.get("molmind-core")
            if plugin:
                _absorb(plugin.limits)

        lo = max([TOP_N_MIN, *mins]) if mins else TOP_N_MIN
        hi = min([TOP_N_MAX, *maxs]) if maxs else TOP_N_MAX
        if lo > hi:
            lo, hi = TOP_N_MIN, TOP_N_MAX
        return lo, hi

    def _plugin_available(self, plugin_id: str, installed: set[str]) -> bool:
        p = self.plugins.get(plugin_id)
        if not p:
            return False
        if p.builtin:
            return True
        return plugin_id in installed

    def settings_view(
        self,
        *,
        profile_id: str = "competition_masld",
        installed_catalog: set[str] | None = None,
    ) -> dict[str, Any]:
        profile = self.get_profile(profile_id)
        installed = installed_catalog or set()
        enabled_plugins = []
        for pid, cfg in profile.plugins.items():
            if bool(cfg.get("enabled", False)):
                enabled_plugins.append(pid)
        # builtin always on if marked
        for p in self.list_builtin():
            if p.plugin_id not in enabled_plugins:
                enabled_plugins.append(p.plugin_id)
        for pid in installed:
            if pid not in enabled_plugins:
                enabled_plugins.append(pid)

        catalog = []
        for p in self.list_catalog():
            catalog.append(
                {
                    "plugin_id": p.plugin_id,
                    "title": p.title,
                    "description": p.description,
                    "source": p.source,
                    "requires": p.requires,
                    "installed": p.plugin_id in installed,
                    "builtin": False,
                    "activation": "user_opt_in",
                    "network_policy": p.network_policy,
                    "capabilities": p.capabilities,
                    "terminology": p.terminology,
                }
            )

        plugins = []
        for p in self.plugins.values():
            is_installed = p.builtin or p.plugin_id in installed
            plugins.append(
                {
                    "id": p.plugin_id,
                    "plugin_id": p.plugin_id,
                    "title": p.title,
                    "description": p.description,
                    "source": p.source,
                    "requires": p.requires,
                    "installed": is_installed,
                    "builtin": p.builtin,
                    "catalog": p.catalog,
                    "can_uninstall": p.catalog and p.plugin_id in installed,
                    "install_target": p.plugin_id if p.catalog else None,
                    "tools": list(p.tools),
                    "skills": list(p.skills),
                    "network_policy": p.network_policy,
                    "capabilities": p.capabilities,
                    "terminology": p.terminology,
                }
            )

        skills = []
        for sid, s in self.skills.items():
            parent = self.plugins.get(s.plugin_id)
            available = self._plugin_available(s.plugin_id, installed)
            profile_on = bool(profile.skills.get(sid, {}).get("enabled", True)) if sid in profile.skills else available
            is_installed = available and (profile_on if sid in profile.skills else available)
            skills.append(
                {
                    "id": s.skill_id,
                    "skill_id": s.skill_id,
                    "title": s.title,
                    "description": s.description,
                    "plugin_id": s.plugin_id,
                    "tools": list(s.tools),
                    "capability_ids": list(s.capability_ids),
                    "installed": is_installed,
                    "builtin": bool(parent and parent.builtin),
                    "catalog": bool(parent and parent.catalog),
                    "can_uninstall": bool(parent and parent.catalog and s.plugin_id in installed),
                    "install_target": s.plugin_id if parent and parent.catalog else None,
                }
            )

        return {
            "profile_id": profile.profile_id,
            "display_name": profile.display_name,
            "catalog_opt_in_only": profile.catalog_opt_in_only,
            "enabled_plugins": enabled_plugins,
            "enabled_skills": [
                sid for sid, cfg in profile.skills.items() if bool(cfg.get("enabled", True))
            ],
            "builtin_plugins": [p.plugin_id for p in self.list_builtin()],
            "catalog": catalog,
            "plugins": plugins,
            "skills": skills,
            "budgets": profile.budgets,
            "policy": profile.policy,
        }


@lru_cache(maxsize=1)
def get_registry() -> AgentRegistry:
    return AgentRegistry()
