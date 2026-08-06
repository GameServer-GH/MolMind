"""Small file-backed query cache and explicit MCP staging store."""
from __future__ import annotations
import json, time, uuid
from pathlib import Path
from typing import Any
from .models import SCPObservation, canonical_hash

class SCPQueryCache:
    def __init__(self, root: Path | None = None, ttl_sec: int = 3600):
        self.root = root or (Path(__file__).resolve().parents[2] / "data" / "scp_hub" / "runtime")
        self.cache_dir, self.staging_dir = self.root / "cache", self.root / "staging"
        self.audit_dir = self.root / "audit"
        self.cache_dir.mkdir(parents=True, exist_ok=True); self.staging_dir.mkdir(parents=True, exist_ok=True); self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_sec = max(0, int(ttl_sec))
    def key(self, *, server_id: str, tool_name: str, schema_hash: str, arguments: dict[str, Any], scope: str = "") -> str:
        return canonical_hash({"scope": scope, "server_id": server_id, "tool_name": tool_name, "schema_hash": schema_hash, "arguments": arguments})[7:]
    def get(self, key: str, *, force_refresh: bool = False) -> dict[str, Any] | None:
        path = self.cache_dir / f"{key}.json"
        if force_refresh or not path.is_file(): return None
        try: item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): return None
        if self.ttl_sec and time.time() - float(item.get("stored_at_epoch") or 0) > self.ttl_sec: return None
        return item
    def put(self, key: str, observation: SCPObservation) -> None:
        payload = {"cache_key": key, "stored_at_epoch": time.time(), "observation": {**observation.__dict__, "content": [block.__dict__ for block in observation.content]}}
        (self.cache_dir / f"{key}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    def stage(self, observation: SCPObservation, *, session_id: str = "", molecule_id: str = "", reason: str = "") -> dict[str, Any]:
        stage_id = uuid.uuid4().hex
        payload = {"stage_id": stage_id, "session_id": session_id, "staged_at_epoch": time.time(), "molecule_id": molecule_id, "reason": reason, "promoted": False, "observation": {**observation.__dict__, "content": [block.__dict__ for block in observation.content]}}
        (self.staging_dir / f"{stage_id}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload
    def list_staging(self, *, session_id: str = "", include_promoted: bool = False) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self.staging_dir.glob("*.json")):
            try: item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError): continue
            if session_id and item.get("session_id") != session_id: continue
            if include_promoted or not item.get("promoted"): out.append(item)
        return out
    def record_call(self, payload: dict[str, Any]) -> dict[str, Any]:
        call_id = str(payload.get("call_id") or uuid.uuid4().hex)
        safe = {**payload, "call_id": call_id, "writes_selection": False, "ranking_changed": False}
        (self.audit_dir / f"{call_id}.json").write_text(json.dumps(safe, ensure_ascii=False), encoding="utf-8")
        return safe
    def list_calls(self, *, session_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self.audit_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try: item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError): continue
            if not session_id or item.get("session_id") == session_id: out.append(item)
            if len(out) >= limit: break
        return out
