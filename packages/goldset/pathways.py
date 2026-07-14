"""加载 NAFLD 通路白名单（机制填空用；不参与排名）。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "data" / "reference" / "nafld_pathways.yaml"


@lru_cache(maxsize=2)
def load_nafld_pathways(path: str | None = None) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_PATH
    if not p.is_file():
        return {"whitelist": [], "positive_pathway_map": {}}
    with p.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return {
        "whitelist": list(data.get("whitelist") or []),
        "positive_pathway_map": dict(data.get("positive_pathway_map") or {}),
    }


def pathway_by_id(pathway_id: str, table: dict[str, Any] | None = None) -> dict[str, Any] | None:
    table = table or load_nafld_pathways()
    for item in table.get("whitelist") or []:
        if str(item.get("id")) == pathway_id:
            return dict(item)
    return None


def infer_pathway_for_positive(name: str, table: dict[str, Any] | None = None) -> dict[str, Any] | None:
    table = table or load_nafld_pathways()
    pid = (table.get("positive_pathway_map") or {}).get(name)
    if not pid:
        return None
    return pathway_by_id(str(pid), table)
