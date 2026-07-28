"""R4：Catalog 适配器 / 空壳 / 主榜隔离。"""

from __future__ import annotations

from agent.registry import AgentRegistry, get_registry
from plugins.aurobind import predict_pl_fitness
from plugins.catalog_dispatch import TOOL_HANDLERS, dispatch_tool, iter_installed_enrichment
from plugins.origene_mcp import mcp_query_opentargets, run_enrichment_pass


def test_catalog_lists_season_and_post_items() -> None:
    get_registry.cache_clear()
    reg = AgentRegistry()
    ids = {p.plugin_id for p in reg.list_catalog()}
    assert "origene-mcp" in ids
    assert "aurobind" in ids
    assert "vcworld" in ids
    assert "enzyme-cage" in ids
    assert "eva-rna" in ids
    for p in reg.list_catalog():
        assert p.builtin is False
        assert p.catalog is True


def test_default_profile_does_not_preenable_catalog() -> None:
    get_registry.cache_clear()
    reg = AgentRegistry()
    for profile_id in ("competition_masld", "minimal", "lab_extensible"):
        view = reg.settings_view(profile_id=profile_id, installed_catalog=set())
        assert all(not c["installed"] for c in view["catalog"])
        assert "molmind-core" in view["enabled_plugins"]
        assert "origene-mcp" not in view["builtin_plugins"]
        assert "vcworld" not in view["builtin_plugins"]
        catalog_ids = {c["plugin_id"] for c in view["catalog"]}
        for pid in catalog_ids:
            assert pid not in view["enabled_plugins"]


def test_only_molmind_core_score_and_rank_writes_selection() -> None:
    get_registry.cache_clear()
    reg = AgentRegistry()
    writers = [t for t in reg.tools.values() if t.writes_selection]
    assert len(writers) == 1
    assert writers[0].tool_id == "score_and_rank"
    assert writers[0].plugin_id == "molmind-core"
    for tool_id in (
        "mcp_query_opentargets",
        "mcp_query_chembl",
        "mcp_query_uniprot",
        "predict_pl_fitness",
    ):
        assert tool_id in reg.tools
        assert reg.tools[tool_id].writes_selection is False


def test_origene_stub_degrades_without_endpoint() -> None:
    out = mcp_query_opentargets("PCSK9")
    assert out["ok"] is True
    assert out["writes_selection"] is False
    assert "origene_mcp_not_configured" in out["degraded"]
    batch = run_enrichment_pass(molecule_ids=["m1"])
    assert batch["writes_selection"] is False
    assert batch["degraded"]


def test_aurobind_stub_degrades_without_enable() -> None:
    out = predict_pl_fitness(smiles_list=["CCO"], target_sequence=None)
    assert out["ok"] is True
    assert out["writes_selection"] is False
    assert "aurobind_not_enabled" in out["degraded"]


def test_dispatch_tool_blocks_selection_write_flag() -> None:
    out = dispatch_tool("mcp_query_chembl", query="aspirin")
    assert out["writes_selection"] is False
    assert "mcp_query_chembl" in TOOL_HANDLERS


def test_installed_enrichment_preserves_selection_hash() -> None:
    from agent.memory import AgentSession

    session = AgentSession(session_id="t", profile_id="competition_masld")
    session.installed_catalog = ["origene-mcp", "aurobind", "vcworld"]
    session.last_selection_sha256 = "abc123"
    session.last_result = None
    results = list(iter_installed_enrichment(session))
    # vcworld is shell → skipped; two adapters run
    assert {p for p, _ in results} == {"origene-mcp", "aurobind"}
    for _, payload in results:
        assert payload["writes_selection"] is False
    assert session.last_selection_sha256 == "abc123"


def test_runtime_enrichment_after_install(monkeypatch, tmp_path) -> None:
    from agent.memory import FileRunStore
    from agent.runtime import loop as loop_mod

    monkeypatch.setenv("MOLMIND_LLM_MECHANISM", "0")
    monkeypatch.setenv("MOLMIND_LLM_NOMINATION_REVIEW", "0")
    monkeypatch.setenv("MOLMIND_LLM_CHAT", "0")
    store = FileRunStore(root=tmp_path / "runs")
    loop_mod._RUNTIME = None
    rt = loop_mod.AgentRuntime(store=store)
    loop_mod._RUNTIME = rt

    session = rt.create_session()
    from pathlib import Path

    sdf = Path(__file__).resolve().parents[2] / "data" / "sample.sdf"
    rt.attach_sdf(session, filename="sample.sdf", content=sdf.read_bytes())
    rt.install_catalog_plugin(session, "origene-mcp")

    events = list(rt.handle_message(session, "生成 top3 提名清单 csv"))
    sha = session.last_selection_sha256
    assert sha
    enrich_ends = [
        e
        for e in events
        if e.get("type") == "tool_end" and e.get("plugin") == "origene-mcp"
    ]
    assert enrich_ends
    assert enrich_ends[0]["digest"]["writes_selection"] is False
    assert enrich_ends[0]["digest"]["selection_sha256_unchanged"] is True
    assert session.last_selection_sha256 == sha
