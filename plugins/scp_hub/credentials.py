"""Credential handling; secrets never appear in serialised observations/logs."""
from __future__ import annotations

import base64
import os
from pathlib import Path

# SCP Hub 发行默认：密钥经 XOR+Base64 混淆嵌入，部署方无需再配环境变量。
# 运营约定：该 key 是允许随代码公开分发的受限 key，权限和额度在服务商侧
# 管理，可随时停用/轮换；禁止授予管理权限或生产数据访问权限。
# 优先级：环境变量 > 本地 .env > 嵌入密钥。设 MOLMIND_SCP_USE_EMBEDDED=0 可禁用嵌入。
_EMBEDDED_KEY_PAD = b"MolMind::SCPHub::MCP::2026"
_EMBEDDED_KEY_BLOB = "PgRBe1xWAVhea3d9cE1SDRd5JTQLFwsBB1dgDAouC15SCQllJmJw"


def _decode_embedded_api_key() -> str:
    if os.environ.get("MOLMIND_SCP_USE_EMBEDDED", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return ""
    try:
        raw = base64.b64decode(_EMBEDDED_KEY_BLOB.encode("ascii"))
        pad = _EMBEDDED_KEY_PAD
        return bytes(b ^ pad[i % len(pad)] for i, b in enumerate(raw)).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return ""


def _local_env_key() -> str:
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.is_file():
        return ""
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() in {"SCP_HUB_API_KEY", "MOLMIND_SCP_HUB_API_KEY"}:
                return value.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def get_api_key(environ: dict[str, str] | None = None) -> tuple[str, str]:
    env = environ if environ is not None else os.environ
    if value := str(env.get("SCP_HUB_API_KEY") or "").strip():
        return value, "SCP_HUB_API_KEY"
    if value := str(env.get("MOLMIND_SCP_HUB_API_KEY") or "").strip():
        return value, "MOLMIND_SCP_HUB_API_KEY"
    # Only when using the live process environment (not an explicit test map).
    if environ is None and (value := _local_env_key()):
        return value, "local_dotenv"
    if environ is None and (value := _decode_embedded_api_key()):
        return value, "embedded"
    return "", "missing"


def credential_status(key: str, *, authorized: bool | None = None) -> str:
    if not key:
        return "missing"
    if authorized is False:
        return "configured_but_unauthorized_for_server"
    if authorized is True:
        return "configured_and_authorized"
    return "configured"
