"""OrigeneMCP Catalog 适配器（默认不启用；须设置中主动添加）。"""

from __future__ import annotations

from plugins.origene_mcp.client import OrigeneMcpClient
from plugins.origene_mcp.tools.enrichment import (
    mcp_query_chembl,
    mcp_query_opentargets,
    mcp_query_uniprot,
    run_enrichment_pass,
)

__all__ = [
    "OrigeneMcpClient",
    "mcp_query_opentargets",
    "mcp_query_chembl",
    "mcp_query_uniprot",
    "run_enrichment_pass",
]
