"""Read-only SCP capability index used for two-level discovery."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

DEFAULT_INDEX = Path(__file__).resolve().parents[2] / "data" / "scp_hub" / "catalog_index.json"

class SCPCatalog:
    def __init__(self, path: Path | None = None):
        self.path = path or DEFAULT_INDEX
        raw = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {"skills": []}
        self.skills = {str(x["skill_id"]): x for x in raw.get("skills", []) if isinstance(x, dict) and x.get("skill_id")}
    def list(self) -> list[dict[str, Any]]: return [dict(x) for x in self.skills.values()]
    def get(self, skill_id: str) -> dict[str, Any]:
        if skill_id not in self.skills: raise KeyError(f"unknown SCP skill: {skill_id}")
        return dict(self.skills[skill_id])
    def search(self, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        words = [w.lower() for w in query.split() if w]
        def score(item: dict[str, Any]) -> int:
            hay = " ".join(str(item.get(k) or "") for k in ("skill_id", "title", "description", "tags")).lower()
            return sum(1 for word in words if word in hay)
        ranked = sorted(self.skills.values(), key=lambda x: (score(x), x.get("skill_id", "")), reverse=True)
        return [dict(x) for x in ranked if not words or score(x) > 0][:limit]
