"""Capability surface assembly for prompt injection."""

from __future__ import annotations

from types import SimpleNamespace

from agent.registry import get_registry
from agent.runtime.capability_context import (
    build_capability_surface,
    format_capability_surface_for_prompt,
)


def test_build_capability_surface_includes_core_and_installable_scp() -> None:
    registry = get_registry()
    session = SimpleNamespace(
        profile_id="competition_masld",
        installed_catalog=[],
        installed_scp_skills={},
    )
    surface = build_capability_surface(registry, session)
    assert surface["profile_id"] == "competition_masld"
    plugin_ids = {item["plugin_id"] for item in surface["installed_plugins"]}
    assert "molmind-core" in plugin_ids
    skill_ids = {item["skill_id"] for item in surface["available_skills"]}
    assert "masld_nominate" in skill_ids
    installable = {
        item["skill_id"] for item in surface["installable_scp_skills"]
    }
    assert "literature_research" in installable
    payload = format_capability_surface_for_prompt(surface)
    assert "literature_research" in payload
    assert "molmind-core" in payload


def test_enabled_scp_skill_marked_enabled_in_surface() -> None:
    registry = get_registry()
    session = SimpleNamespace(
        profile_id="competition_masld",
        installed_catalog=["scp-hub"],
        installed_scp_skills={
            "literature_research": {
                "title": "Scholar / PubMed Literature Research",
                "enabled": True,
                "tools": ["scp:Scholar-KG:query_paper"],
                "credential_status": "configured_and_authorized",
            }
        },
    )
    surface = build_capability_surface(registry, session)
    lit = next(
        item
        for item in surface["installable_scp_skills"]
        if item["skill_id"] == "literature_research"
    )
    assert lit["installed"] is True
    assert lit["enabled"] is True
    assert surface["installed_scp_skills"][0]["skill_id"] == "literature_research"


def test_capability_surface_includes_locked_nomination_csv_contract() -> None:
    from plugins.molmind_core.scientific.pipeline.export import CSV_COLUMNS

    registry = get_registry()
    session = SimpleNamespace(
        profile_id="competition_masld",
        installed_catalog=[],
        installed_scp_skills={},
        sdf_bytes=b"x",
        sdf_filename="lib.sdf",
    )
    surface = build_capability_surface(registry, session)
    csv_fact = surface["nomination_csv"]
    assert csv_fact["schema_locked"] is True
    assert csv_fact["user_selectable_columns"] is False
    assert csv_fact["column_count"] == len(CSV_COLUMNS)
    assert csv_fact["columns_preview"][:5] == list(CSV_COLUMNS[:5])
    assert "排名" in csv_fact["columns_preview"]
    option_ids = {item["id"] for item in surface["discussable_execution_options"]}
    assert {"top_n", "export_csv", "mechanism_pdf", "enrich_aurobind"} <= option_ids
    payload = format_capability_surface_for_prompt(surface)
    assert "schema_locked" in payload
    assert "user_selectable_columns" in payload
    assert "discussable_execution_options" in payload
    assert any("指定附加列" in note for note in surface["policy_notes"])


def test_export_nomination_tool_meta_mentions_locked_schema() -> None:
    from agent.registry import TOOL_META, get_registry

    get_registry.cache_clear()
    registry = get_registry()
    desc = TOOL_META["export_nomination"]["description"]
    assert "锁定" in desc or "schema" in desc.lower()
    tool = registry.tools.get("export_nomination")
    assert tool is not None
    assert "锁定" in str(tool.description) or "schema" in str(tool.description).lower()
