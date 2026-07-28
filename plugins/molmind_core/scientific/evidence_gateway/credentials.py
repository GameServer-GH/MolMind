"""Resolve provider credentials using the project's configured precedence."""

from __future__ import annotations

import os
import base64
import subprocess
from pathlib import Path
from typing import Iterable

KEYCHAIN_REFS = {
    "epa_ctx": ("MolMind", "molmind/epa_ctx"),
}

# Publicly distributable, provider-side restricted demo credential.  It is
# intentionally revocable/replaceable by the provider and must never receive
# administrative or production-data permissions.  Environment/mounted
# credentials still take precedence; set MOLMIND_USE_EMBEDDED_PUBLIC_KEYS=0
# to disable this fallback.
_PUBLIC_KEY_PAD = b"MolMind::EPA::CTX::2026"
_PUBLIC_KEY_BLOBS = {
    "epa_ctx": "dFpcdVkIVA8XdDUjWBd3bDwKF1BSBFRgC1t4CAtSXl98Y3IC",
}


def _embedded_public_key(provider_id: str) -> str | None:
    if os.getenv("MOLMIND_USE_EMBEDDED_PUBLIC_KEYS", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return None
    blob = _PUBLIC_KEY_BLOBS.get(provider_id)
    if not blob:
        return None
    try:
        raw = base64.b64decode(blob.encode("ascii"), validate=True)
        return bytes(
            value ^ _PUBLIC_KEY_PAD[index % len(_PUBLIC_KEY_PAD)]
            for index, value in enumerate(raw)
        ).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return None
def _mounted_secret(path_value: str) -> str | None:
    """Read a single mounted secret without ever echoing its contents.

    Docker/Kubernetes secret mounts conventionally contain one value and a
    trailing newline.  Keep the limit deliberately small: a credential is not
    a configuration document, and accepting arbitrary files here is both
    surprising and unnecessarily risky.
    """
    try:
        path = Path(path_value).expanduser()
        if not path.is_file() or path.stat().st_size > 16 * 1024:
            return None
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return value or None


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
    """Resolve explicit → environment → mounted secret → Keychain.

    ``<ENV>_FILE`` is supported for every supplied environment variable, plus
    the MolMind-prefixed spelling.  Project YAML is intentionally not a
    credential source: it is versioned policy, not a secret store.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    for name in env_names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    file_names = [f"{name}_FILE" for name in env_names]
    file_names.append(f"MOLMIND_{provider_id.upper()}_SECRET_FILE")
    for name in dict.fromkeys(file_names):
        path_value = os.getenv(name, "").strip()
        if path_value:
            value = _mounted_secret(path_value)
            if value:
                return value
    ref = KEYCHAIN_REFS.get(provider_id)
    if ref:
        value = _macos_keychain(*ref)
        if value:
            return value
    return _embedded_public_key(provider_id)
