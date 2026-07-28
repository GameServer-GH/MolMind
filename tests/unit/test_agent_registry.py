"""Agent Registry / Catalog / Profile 加载测试。"""

from __future__ import annotations

from agent.registry import AgentRegistry, get_registry


def test_competition_profile_loads_builtin_only_enabled() -> None:
    get_registry.cache_clear()
    reg = AgentRegistry()
    profile = reg.get_profile("competition_masld")
    assert profile.catalog_opt_in_only is True
    assert profile.plugins["molmind-core"]["enabled"] is True

    view = reg.settings_view(profile_id="competition_masld", installed_catalog=set())
    assert "molmind-core" in view["builtin_plugins"]
    assert "molmind-core" in view["enabled_plugins"]
    # Catalog present but not installed / not treated as builtin
    catalog_ids = {c["plugin_id"] for c in view["catalog"]}
    assert "origene-mcp" in catalog_ids
    assert "aurobind" in catalog_ids
    assert "vcworld" in catalog_ids
    for item in view["catalog"]:
        assert item["builtin"] is False
        assert item["activation"] == "user_opt_in"
        assert item["installed"] is False


def test_score_and_rank_is_only_selection_writer_in_registry() -> None:
    get_registry.cache_clear()
    reg = AgentRegistry()
    writers = [t for t in reg.tools.values() if t.writes_selection]
    assert len(writers) == 1
    assert writers[0].tool_id == "score_and_rank"
    assert writers[0].plugin_id == "molmind-core"


def test_catalog_install_marks_installed(client=None) -> None:
    # pure unit without fastapi
    from agent import get_runtime

    # reset singleton-ish store sessions by creating fresh runtime store usage
    rt = get_runtime()
    session = rt.create_session()
    assert "origene-mcp" not in session.installed_catalog
    rt.install_catalog_plugin(session, "origene-mcp")
    view = rt.settings_view(session)
    item = next(c for c in view["catalog"] if c["plugin_id"] == "origene-mcp")
    assert item["installed"] is True
    assert "origene-mcp" in view["enabled_plugins"]
