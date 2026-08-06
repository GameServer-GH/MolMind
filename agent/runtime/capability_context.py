"""Live capability surface for LLM chat / planner prompts.

This is context assembly only — no dialog-act keyword routing.
The model decides how to use the surface once it can see it.
"""

from __future__ import annotations

import json
from typing import Any

# Keep export facts aligned with plugins.molmind_core.scientific.pipeline.export.
# Import lazily inside helpers so unit tests can still mock CSV_COLUMNS.


def _nomination_csv_export_facts() -> dict[str, Any]:
    """Factual, locked nomination-CSV contract for prompt injection."""
    try:
        from plugins.molmind_core.scientific.pipeline.export import CSV_COLUMNS
    except Exception:  # noqa: BLE001 — surface must stay available offline
        CSV_COLUMNS = [
            "排名",
            "化合物标识符",
            "降脂依据",
            "毒性判断",
            "排序理由",
        ]
    columns = [str(name) for name in (CSV_COLUMNS or []) if str(name)]
    preview = columns[:12]
    return {
        "artifact": "nomination_csv",
        "schema_locked": True,
        "user_selectable_columns": False,
        "column_count": len(columns),
        "columns_preview": preview,
        "notes": [
            "提名 CSV 列集合与顺序为导出契约，执行时不可按用户口头指定增减列。",
            "禁止编造 Rank/ID/SMILES/MASLD_Score/Toxicity_Risk/Note 等简化英文字段当作默认列。",
            "旁证 skill（如 enrich_topn_with_aurobind、enrich_mechanism_with_mcp）"
            "可另跑并产生旁证产物，但不改冻结主榜，也不改提名 CSV schema。",
        ],
    }


def _discussable_execution_options() -> list[dict[str, str]]:
    """Options the chat model may ask users to confirm before execute."""
    return [
        {
            "id": "top_n",
            "description": "候选数量 TopN（须在技能/工具上限内）",
        },
        {
            "id": "export_csv",
            "description": "是否导出提名 CSV（schema 锁定，不可自定义列）",
        },
        {
            "id": "mechanism_pdf",
            "description": "是否生成机制与后续验证建议 PDF",
        },
        {
            "id": "submission_bundle",
            "description": "是否打包结果包（CSV/候补/审计等）",
        },
        {
            "id": "enrich_aurobind",
            "description": "是否另跑 AuroBind fitness 旁证（不改主榜、不改 CSV 列）",
        },
        {
            "id": "enrich_mcp",
            "description": "是否另跑 OrigeneMCP 机制旁证（不改主榜、不改 CSV 列）",
        },
    ]


def build_capability_surface(
    registry: Any,
    session: Any | None = None,
    *,
    scp_catalog: Any | None = None,
) -> dict[str, Any]:
    """Compact, factual capability view for prompt injection."""
    installed_catalog = set(getattr(session, "installed_catalog", None) or [])
    profile_id = str(getattr(session, "profile_id", None) or "competition_masld")
    view = registry.settings_view(
        profile_id=profile_id,
        installed_catalog=installed_catalog,
    )

    installed_scp = getattr(session, "installed_scp_skills", None) or {}
    if not isinstance(installed_scp, dict):
        installed_scp = {}

    plugins: list[dict[str, Any]] = []
    for item in view.get("plugins") or []:
        if not isinstance(item, dict):
            continue
        plugins.append(
            {
                "plugin_id": item.get("plugin_id"),
                "title": item.get("title"),
                "description": str(item.get("description") or "")[:220],
                "builtin": bool(item.get("builtin")),
                "installed": bool(item.get("installed")),
                "catalog": bool(item.get("catalog")),
            }
        )

    skills: list[dict[str, Any]] = []
    for item in view.get("skills") or []:
        if not isinstance(item, dict) or not item.get("installed"):
            continue
        skills.append(
            {
                "skill_id": item.get("skill_id"),
                "title": item.get("title"),
                "description": str(item.get("description") or "")[:220],
                "plugin_id": item.get("plugin_id"),
                "tools": list(item.get("tools") or [])[:12],
                "builtin": bool(item.get("builtin")),
            }
        )

    scp_installed: list[dict[str, Any]] = []
    for skill_id, state in installed_scp.items():
        if not isinstance(state, dict):
            continue
        scp_installed.append(
            {
                "skill_id": str(skill_id),
                "title": str(state.get("title") or skill_id),
                "enabled": bool(state.get("enabled", True)),
                "tools": list(state.get("tools") or [])[:12],
                "credential_status": str(state.get("credential_status") or ""),
            }
        )

    # Prefer plugin-declared capabilities (planner-facing) and merge catalog index.
    installable_scp: list[dict[str, Any]] = []
    seen_skill_ids: set[str] = set()
    plugin = registry.plugins.get("scp-hub") if registry else None
    for capability in getattr(plugin, "capabilities", None) or []:
        if not isinstance(capability, dict):
            continue
        skill_id = str(capability.get("skill_id") or "")
        if not skill_id or skill_id in seen_skill_ids:
            continue
        seen_skill_ids.add(skill_id)
        installable_scp.append(
            {
                "skill_id": skill_id,
                "capability_id": str(capability.get("capability_id") or ""),
                "title": str(capability.get("title") or skill_id),
                "domains": list(capability.get("domains") or [])[:8],
                "supports": list(capability.get("supports") or [])[:8],
                "installed": skill_id in installed_scp,
                "enabled": bool(
                    installed_scp.get(skill_id, {}).get("enabled", False)
                )
                if skill_id in installed_scp
                else False,
            }
        )

    catalog_source = scp_catalog
    if catalog_source is None and hasattr(registry, "scp"):
        catalog_source = getattr(getattr(registry, "scp", None), "catalog", None)
    list_fn = getattr(catalog_source, "list", None)
    if callable(list_fn):
        for item in list_fn() or []:
            if not isinstance(item, dict):
                continue
            skill_id = str(item.get("skill_id") or "")
            if not skill_id or skill_id in seen_skill_ids:
                continue
            seen_skill_ids.add(skill_id)
            installable_scp.append(
                {
                    "skill_id": skill_id,
                    "capability_id": "",
                    "title": str(item.get("title") or skill_id),
                    "description": str(item.get("description") or "")[:180],
                    "installed": skill_id in installed_scp,
                    "enabled": bool(
                        installed_scp.get(skill_id, {}).get("enabled", False)
                    )
                    if skill_id in installed_scp
                    else False,
                }
            )

    catalog_plugins = [
        {
            "plugin_id": item.get("plugin_id"),
            "title": item.get("title"),
            "description": str(item.get("description") or "")[:180],
            "installed": bool(item.get("installed")),
        }
        for item in view.get("catalog") or []
        if isinstance(item, dict)
    ]

    has_sdf = bool(getattr(session, "sdf_bytes", None)) if session is not None else False
    sdf_filename = str(getattr(session, "sdf_filename", None) or "") if session else ""
    nomination_csv = _nomination_csv_export_facts()
    discussable = _discussable_execution_options()

    return {
        "profile_id": profile_id,
        "session_library": {
            "has_sdf": has_sdf,
            "sdf_filename": sdf_filename if has_sdf else "",
        },
        "nomination_csv": nomination_csv,
        "discussable_execution_options": discussable,
        "installed_plugins": plugins,
        "available_skills": skills,
        "installed_scp_skills": scp_installed,
        "installable_scp_skills": installable_scp,
        "catalog_plugins": catalog_plugins,
        "policy_notes": [
            "未安装的 SCP skill 需用户确认安装后才能调用；安装成功后留在当前对话继续，无需重发原提示词。",
            "SCP / MCP 实时结果仅作补充证据，不得改写或重算冻结主榜排名。",
            "本地核心筛选（SDF → 排序 → CSV / 机制 PDF）依赖会话附件与已注册 skill；"
            "session_library.has_sdf 为真时不得声称缺少化合物库。",
            "提名 CSV schema 锁定（nomination_csv.schema_locked=true）；"
            "禁止声称用户可在执行时指定附加列，禁止编造默认英文字段名。",
            "讨论执行条件时，只使用 discussable_execution_options 中的选项。",
        ],
    }


def format_capability_surface_for_prompt(
    surface: dict[str, Any],
    *,
    max_chars: int = 5200,
) -> str:
    """Serialize the surface for system/user prompts."""
    payload = json.dumps(surface, ensure_ascii=False, separators=(",", ":"))
    if len(payload) <= max_chars:
        return payload
    # Prefer keeping export contract + installable/installed SCP over long blurbs.
    compact = {
        "profile_id": surface.get("profile_id"),
        "session_library": surface.get("session_library") or {},
        "nomination_csv": surface.get("nomination_csv") or {},
        "discussable_execution_options": surface.get(
            "discussable_execution_options"
        )
        or [],
        "installed_plugins": [
            {
                "plugin_id": item.get("plugin_id"),
                "title": item.get("title"),
                "installed": item.get("installed"),
                "builtin": item.get("builtin"),
            }
            for item in surface.get("installed_plugins") or []
        ],
        "available_skills": [
            {
                "skill_id": item.get("skill_id"),
                "title": item.get("title"),
                "tools": item.get("tools"),
            }
            for item in surface.get("available_skills") or []
        ],
        "installed_scp_skills": surface.get("installed_scp_skills") or [],
        "installable_scp_skills": [
            {
                "skill_id": item.get("skill_id"),
                "capability_id": item.get("capability_id"),
                "title": item.get("title"),
                "installed": item.get("installed"),
                "enabled": item.get("enabled"),
            }
            for item in surface.get("installable_scp_skills") or []
        ],
        "catalog_plugins": [
            {
                "plugin_id": item.get("plugin_id"),
                "title": item.get("title"),
                "installed": item.get("installed"),
            }
            for item in surface.get("catalog_plugins") or []
        ],
        "policy_notes": surface.get("policy_notes") or [],
    }
    payload = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(payload) <= max_chars:
        return payload
    return payload[: max_chars - 1] + "…"
