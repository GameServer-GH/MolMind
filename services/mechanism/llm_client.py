"""OpenAI 兼容 Chat Completions 客户端（机制润色专用；不参与排序）。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_DIR = ROOT / "data" / "llm_cache"

# DeepSeek 交付默认：密钥经 XOR+Base64 混淆嵌入，部署方无需再配环境变量。
# 优先级：环境变量 > 嵌入密钥。设 MOLMIND_LLM_USE_EMBEDDED=0 可禁用嵌入（测试用）。
# 注意：源码内混淆可被逆向；公开仓库仍有泄露风险，赛后建议轮换 Key。
_EMBEDDED_KEY_PAD = b"MolMind::DeepSeek::Mechanism::2026"
_EMBEDDED_KEY_BLOB = "PgRBfFxcAV4DIFRTQTVXUVoPX3UEUQ0CCwgQWghYVAADBXk="


def _decode_embedded_api_key() -> str:
    if os.environ.get("MOLMIND_LLM_USE_EMBEDDED", "1").strip().lower() in {
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


@dataclass(frozen=True)
class LLMSettings:
    """从配置 + 环境变量 + 嵌入密钥解析；密钥不写入 config_hash / 日志明文。"""

    enabled: bool
    model: str
    base_url: str
    api_key: str
    temperature: float
    timeout_sec: float
    max_tokens: int
    cache_dir: Path
    use_cache: bool

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.api_key) and bool(self.model)


def resolve_llm_settings(llm_cfg: dict[str, Any] | None) -> LLMSettings:
    cfg = dict(llm_cfg or {})
    enabled = bool(cfg.get("enabled", False)) and bool(cfg.get("mechanism_pdf", True))
    # 环境变量可强制开关（不改 YAML 即可试跑）
    env_flag = os.environ.get("MOLMIND_LLM_MECHANISM", "").strip().lower()
    if env_flag in {"1", "true", "yes", "on"}:
        enabled = True
    elif env_flag in {"0", "false", "no", "off"}:
        enabled = False

    api_key = (
        os.environ.get("MOLMIND_LLM_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or _decode_embedded_api_key()
        or ""
    ).strip()
    base_url = (
        os.environ.get("MOLMIND_LLM_BASE_URL")
        or str(cfg.get("base_url") or "https://api.deepseek.com/v1")
    ).rstrip("/")
    # DeepSeek OpenAI 兼容层：宿主为 api.deepseek.com 时统一走 /v1
    if base_url in {"https://api.deepseek.com", "http://api.deepseek.com"}:
        base_url = "https://api.deepseek.com/v1"
    model = (
        os.environ.get("MOLMIND_LLM_MODEL")
        or str(cfg.get("model") or "deepseek-v4-pro")
    ).strip()
    cache_raw = cfg.get("cache_dir") or "data/llm_cache"
    cache_dir = Path(cache_raw)
    if not cache_dir.is_absolute():
        cache_dir = ROOT / cache_dir

    return LLMSettings(
        enabled=enabled,
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=float(cfg.get("temperature", 0)),
        timeout_sec=float(cfg.get("timeout_sec", 60)),
        max_tokens=int(cfg.get("max_tokens", 4096)),
        cache_dir=cache_dir,
        use_cache=bool(cfg.get("use_cache", True)),
    )


def _cache_key(model: str, system: str, user: str, temperature: float) -> str:
    blob = json.dumps(
        {"model": model, "system": system, "user": user, "temperature": temperature},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _read_cache(cache_dir: Path, key: str) -> str | None:
    path = cache_dir / f"{key}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        text = data.get("content")
        return str(text) if text else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _write_cache(cache_dir: Path, key: str, *, model: str, content: str) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{key}.json"
    path.write_text(
        json.dumps(
            {"model": model, "content": content},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


class MechanismLLMError(RuntimeError):
    """机制 LLM 调用失败（调用方应降级到模板）。"""


def chat_completion(
    settings: LLMSettings,
    *,
    system: str,
    user: str,
) -> str:
    """调用 OpenAI 兼容 /chat/completions；命中磁盘缓存则直接返回。"""
    if not settings.ready:
        raise MechanismLLMError("LLM 未就绪（未启用或缺少 API Key）")

    key = _cache_key(settings.model, system, user, settings.temperature)
    if settings.use_cache:
        cached = _read_cache(settings.cache_dir, key)
        if cached:
            return cached

    url = f"{settings.base_url}/chat/completions"
    payload = {
        "model": settings.model,
        "temperature": settings.temperature,
        "max_tokens": settings.max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=settings.timeout_sec) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise MechanismLLMError(f"HTTP 失败: {exc}") from exc
    except (ValueError, TypeError) as exc:
        raise MechanismLLMError(f"响应解析失败: {exc}") from exc

    try:
        message = data["choices"][0]["message"]
        content = message.get("content")
        # deepseek-v4-pro 等推理模型：reasoning 占 completion 额度，max_tokens 过小会得到空 content
        if not (content or "").strip():
            reasoning = (message.get("reasoning_content") or "").strip()
            if reasoning:
                raise MechanismLLMError(
                    "模型仅返回 reasoning_content、content 为空；"
                    "请增大 llm.max_tokens（推理模型需预留 reasoning 额度）"
                )
    except MechanismLLMError:
        raise
    except (KeyError, IndexError, TypeError) as exc:
        raise MechanismLLMError(f"响应缺少 content: {exc}") from exc
    text = str(content or "").strip()
    if not text:
        raise MechanismLLMError("模型返回空内容")

    if settings.use_cache:
        _write_cache(settings.cache_dir, key, model=settings.model, content=text)
    return text
