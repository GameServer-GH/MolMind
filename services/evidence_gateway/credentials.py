"""Resolve provider credentials using the project's configured precedence."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterable

import yaml

KEYCHAIN_REFS = {
    "epa_ctx": ("MolMind", "molmind/epa_ctx"),
}


def _macos_keychain(account: str, service: str) -> str | None:
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                account,
                "-s",
                service,
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value or None


def resolve_secret(
    provider_id: str,
    *,
    explicit: str | None = None,
    env_names: Iterable[str] = (),
) -> str | None:
    """Resolve explicit → environment → plaintext project config → OS Keychain."""
    if explicit and explicit.strip():
        return explicit.strip()
    for name in env_names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    provider_config = Path(__file__).resolve().parents[2] / "configs/evidence_providers.yaml"
    if provider_config.is_file():
        payload = yaml.safe_load(provider_config.read_text(encoding="utf-8")) or {}
        value = (
            ((payload.get("providers") or {}).get(provider_id) or {}).get("api_key")
        )
        if value and str(value).strip():
            return str(value).strip()
    ref = KEYCHAIN_REFS.get(provider_id)
    if ref:
        return _macos_keychain(*ref)
    return None
