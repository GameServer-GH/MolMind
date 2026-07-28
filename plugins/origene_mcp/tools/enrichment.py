"""OrigeneMCP enrichment Tools — 只读旁证，永不写主榜。"""

from __future__ import annotations

from typing import Any

from plugins.catalog_common import assert_no_selection_write, enrichment_envelope
from plugins.origene_mcp.client import OrigeneMcpClient


def _query(tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    client = OrigeneMcpClient()
    remote = client.query(tool, payload)
    if not remote.get("ok"):
        degraded = [str(remote.get("degraded") or "origene_mcp_unavailable")]
        result = enrichment_envelope(
            tool=tool,
            plugin="origene-mcp",
            ok=True,  # 调用本身成功完成（已降级）
            data=None,
            message="OrigeneMCP 不可用或未配置；已降级，不影响主榜与结果导出。",
            degraded=degraded,
            digest={"configured": client.configured},
        )
        assert_no_selection_write(result)
        return result
    result = enrichment_envelope(
        tool=tool,
        plugin="origene-mcp",
        ok=True,
        data=remote.get("data"),
        message="OrigeneMCP enrichment 完成（旁证，不改排名）。",
        digest={"configured": True},
    )
    assert_no_selection_write(result)
    return result


def mcp_query_opentargets(query: str = "", **_: Any) -> dict[str, Any]:
    return _query("mcp_query_opentargets", {"query": query})


def mcp_query_chembl(query: str = "", **_: Any) -> dict[str, Any]:
    return _query("mcp_query_chembl", {"query": query})


def mcp_query_uniprot(query: str = "", **_: Any) -> dict[str, Any]:
    return _query("mcp_query_uniprot", {"query": query})


def run_enrichment_pass(
    *,
    molecule_ids: list[str] | None = None,
    focus: str = "mechanism",
) -> dict[str, Any]:
    """一次轻量 enrichment 巡检：对 OpenTargets/ChEMBL/UniProt 做探测式查询。"""
    ids = molecule_ids or []
    seed = ids[0] if ids else focus
    steps = [
        mcp_query_opentargets(seed),
        mcp_query_chembl(seed),
        mcp_query_uniprot(seed),
    ]
    degraded: list[str] = []
    for s in steps:
        degraded.extend(s.get("degraded") or [])
    result = enrichment_envelope(
        tool="enrich_mechanism_with_mcp",
        plugin="origene-mcp",
        ok=True,
        data={"steps": steps, "molecule_ids": ids},
        message=(
            "OrigeneMCP enrichment 巡检完成（旁证）。"
            + (" 已降级：" + ",".join(sorted(set(degraded))) if degraded else "")
        ),
        degraded=sorted(set(degraded)),
        digest={"n_steps": len(steps)},
    )
    assert_no_selection_write(result)
    return result
