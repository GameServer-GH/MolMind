"""已安装 Catalog 插件的 Tool 分发（enrichment only）。"""

from __future__ import annotations

from typing import Any, Callable, Iterator

from agent.memory import AgentSession
from plugins.catalog_common import assert_no_selection_write

Handler = Callable[..., dict[str, Any]]


def _origene_pass(**kwargs: Any) -> dict[str, Any]:
    from plugins.origene_mcp import run_enrichment_pass

    return run_enrichment_pass(**kwargs)


def _aurobind_pass(**kwargs: Any) -> dict[str, Any]:
    from plugins.aurobind import run_enrichment_pass

    return run_enrichment_pass(**kwargs)


# plugin_id → callable enrichment pass
ENRICHMENT_PASSES: dict[str, Handler] = {
    "origene-mcp": _origene_pass,
    "aurobind": _aurobind_pass,
}

TOOL_HANDLERS: dict[str, Handler] = {
    "mcp_query_opentargets": lambda **kw: __import__(
        "plugins.origene_mcp", fromlist=["mcp_query_opentargets"]
    ).mcp_query_opentargets(**kw),
    "mcp_query_chembl": lambda **kw: __import__(
        "plugins.origene_mcp", fromlist=["mcp_query_chembl"]
    ).mcp_query_chembl(**kw),
    "mcp_query_uniprot": lambda **kw: __import__(
        "plugins.origene_mcp", fromlist=["mcp_query_uniprot"]
    ).mcp_query_uniprot(**kw),
    "predict_pl_fitness": lambda **kw: __import__(
        "plugins.aurobind", fromlist=["predict_pl_fitness"]
    ).predict_pl_fitness(**kw),
}


def dispatch_tool(tool_id: str, **kwargs: Any) -> dict[str, Any]:
    if tool_id not in TOOL_HANDLERS:
        raise KeyError(f"未知 Catalog tool: {tool_id}")
    result = TOOL_HANDLERS[tool_id](**kwargs)
    assert_no_selection_write(result)
    if result.get("writes_selection"):
        raise RuntimeError("catalog tool blocked from writing selection")
    return result


def iter_installed_enrichment(
    session: AgentSession,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """对会话已安装且有适配器的 Catalog 插件执行 enrichment pass。"""
    smiles: list[str] = []
    mol_ids: list[str] = []
    result = session.last_result
    if result is not None:
        for mol in getattr(result, "top_molecules", None) or []:
            sid = getattr(mol, "smiles", None)
            mid = getattr(mol, "molecule_id", None)
            if sid:
                smiles.append(str(sid))
            if mid:
                mol_ids.append(str(mid))

    for plugin_id in list(session.installed_catalog):
        handler = ENRICHMENT_PASSES.get(plugin_id)
        if handler is None:
            # 空壳 Catalog：跳过，不报错
            continue
        kwargs: dict[str, Any] = {}
        if plugin_id == "origene-mcp":
            kwargs["molecule_ids"] = mol_ids
        elif plugin_id == "aurobind":
            kwargs["smiles_list"] = smiles
            kwargs["target_sequence"] = None
        out = handler(**kwargs)
        assert_no_selection_write(out)
        yield plugin_id, out
