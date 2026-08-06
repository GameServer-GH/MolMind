"""Credential handling; secrets never appear in serialised observations/logs."""
from __future__ import annotations
import os
from pathlib import Path

def _local_env_key() -> str:
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.is_file(): return ""
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            name, value = line.split("=", 1)
            if name.strip() == "SCP_HUB_API_KEY": return value.strip().strip('"').strip("'")
    except OSError: return ""
    return ""

def get_api_key(environ: dict[str, str] | None = None) -> tuple[str, str]:
    env = environ if environ is not None else os.environ
    if (value := str(env.get("SCP_HUB_API_KEY") or "").strip()):
        return value, "SCP_HUB_API_KEY"
    if (value := str(env.get("MOLMIND_SCP_HUB_API_KEY") or "").strip()):
        return value, "MOLMIND_SCP_HUB_API_KEY"
    if environ is None and (value := _local_env_key()):
        return value, "local_dotenv"
    return "", "missing"

def credential_status(key: str, *, authorized: bool | None = None) -> str:
    if not key: return "missing"
    if authorized is False: return "configured_but_unauthorized_for_server"
    if authorized is True: return "configured_and_authorized"
    return "configured"
