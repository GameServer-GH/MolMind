"""OpenAI 兼容 Chat Completions 客户端（机制润色专用；不参与排序）。"""

from __future__ import annotations

from plugins.molmind_core.scientific.paths import REPO_ROOT
import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import httpx

ROOT = REPO_ROOT
DEFAULT_CACHE_DIR = ROOT / "data" / "llm_cache"

# DeepSeek 发行默认：密钥经 XOR+Base64 混淆嵌入，部署方无需再配环境变量。
# 运营约定：该 key 是允许随代码公开分发的受限 key，权限和额度在服务商侧
# 管理，可随时停用/轮换；禁止授予管理权限或生产数据访问权限。
# 优先级：环境变量 > 嵌入密钥。设 MOLMIND_LLM_USE_EMBEDDED=0 可禁用嵌入。
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
    """从配置 + 环境变量 + 公开受限嵌入 key 解析；不写入 config_hash / 日志。"""

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


def resolve_llm_settings(
    llm_cfg: dict[str, Any] | None,
    *,
    purpose: str = "mechanism",
) -> LLMSettings:
    """Resolve LLM settings.

    ``purpose``:
    - ``mechanism``: requires ``llm.enabled`` and ``mechanism_pdf`` (or env override)
    - ``nomination_review``: requires ``llm.enabled`` and ``nomination_review``
      (default True when enabled; env ``MOLMIND_LLM_NOMINATION_REVIEW`` overrides)
    - ``agent_chat``: conversational Q&A / planning; default model ``deepseek-v4-pro``
      (env ``MOLMIND_LLM_CHAT`` can force on/off; ``MOLMIND_LLM_MODEL`` overrides model)
    - ``agent_reflection``: fast Reflection Gate reviewer; default model
      ``deepseek-v4-flash`` (env ``MOLMIND_LLM_REFLECTION`` / ``MOLMIND_LLM_REFLECTION_MODEL``)
    """
    cfg = dict(llm_cfg or {})
    base_enabled = bool(cfg.get("enabled", False))

    if purpose == "nomination_review":
        enabled = base_enabled and bool(cfg.get("nomination_review", True))
        env_flag = os.environ.get("MOLMIND_LLM_NOMINATION_REVIEW", "").strip().lower()
        if env_flag in {"1", "true", "yes", "on"}:
            enabled = True
        elif env_flag in {"0", "false", "no", "off"}:
            enabled = False
    elif purpose == "agent_chat":
        enabled = bool(cfg.get("agent_chat", True))
        env_flag = os.environ.get("MOLMIND_LLM_CHAT", "").strip().lower()
        if env_flag in {"1", "true", "yes", "on"}:
            enabled = True
        elif env_flag in {"0", "false", "no", "off"}:
            enabled = False
    elif purpose == "agent_reflection":
        # Follow the Reflection Gate mode by default: on unless explicitly off.
        enabled = bool(cfg.get("agent_reflection", True))
        env_flag = os.environ.get("MOLMIND_LLM_REFLECTION", "").strip().lower()
        if env_flag in {"1", "true", "yes", "on"}:
            enabled = True
        elif env_flag in {"0", "false", "no", "off"}:
            enabled = False
        gate_mode = os.environ.get("MOLMIND_REFLECTION_GATE", "").strip().lower()
        if gate_mode in {"off", "0", "false", "no"}:
            enabled = False
    else:
        enabled = base_enabled and bool(cfg.get("mechanism_pdf", True))
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
    if purpose == "agent_reflection":
        base_url = (
            os.environ.get("MOLMIND_LLM_REFLECTION_BASE_URL")
            or os.environ.get("MOLMIND_LLM_BASE_URL")
            or str(cfg.get("reflection_base_url") or cfg.get("base_url") or "")
            or "https://api.deepseek.com/v1"
        ).rstrip("/")
    else:
        base_url = (
            os.environ.get("MOLMIND_LLM_BASE_URL")
            or str(cfg.get("base_url") or "https://api.deepseek.com/v1")
        ).rstrip("/")
    # DeepSeek OpenAI 兼容层：宿主为 api.deepseek.com 时统一走 /v1
    if base_url in {"https://api.deepseek.com", "http://api.deepseek.com"}:
        base_url = "https://api.deepseek.com/v1"

    if purpose == "agent_reflection":
        # Flash is intentional for Gate 2 latency; do not inherit MOLMIND_LLM_MODEL
        # (which stays on deepseek-v4-pro for normal agent / chat / planning).
        model = (
            os.environ.get("MOLMIND_LLM_REFLECTION_MODEL")
            or str(cfg.get("reflection_model") or "deepseek-v4-flash")
        ).strip()
        temperature = float(
            cfg.get(
                "reflection_temperature",
                cfg.get("temperature", 0),
            )
        )
        timeout_sec = float(
            os.environ.get("MOLMIND_LLM_REFLECTION_TIMEOUT_SEC")
            or cfg.get("reflection_timeout_sec", 6)
        )
        max_tokens = int(
            os.environ.get("MOLMIND_LLM_REFLECTION_MAX_TOKENS")
            or cfg.get("reflection_max_tokens", 512)
        )
        use_cache = bool(cfg.get("reflection_use_cache", False))
    else:
        model = (
            os.environ.get("MOLMIND_LLM_MODEL")
            or str(cfg.get("model") or "deepseek-v4-pro")
        ).strip()
        temperature = float(cfg.get("temperature", 0))
        timeout_sec = float(cfg.get("timeout_sec", 60))
        max_tokens = int(cfg.get("max_tokens", 4096))
        use_cache = bool(cfg.get("use_cache", True))

    cache_raw = cfg.get("cache_dir") or "data/llm_cache"
    cache_dir = Path(cache_raw)
    if not cache_dir.is_absolute():
        cache_dir = ROOT / cache_dir

    return LLMSettings(
        enabled=enabled,
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
        timeout_sec=timeout_sec,
        max_tokens=max_tokens,
        cache_dir=cache_dir,
        use_cache=use_cache,
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
    cancel_event: Any = None,
) -> str:
    """调用 OpenAI 兼容 /chat/completions；命中磁盘缓存则直接返回。

    When ``cancel_event`` (or the active cancel ContextVar) is set, the HTTP
    call is abandoned promptly and late responses are discarded.
    """
    if not settings.ready:
        raise MechanismLLMError("LLM 未就绪（未启用或缺少 API Key）")

    try:
        from agent.runtime.cancellable_call import (
            CallCancelled,
            resolve_cancel_event,
            run_cancellable,
        )
    except Exception:  # noqa: BLE001 — keep mechanism usable outside agent runtime
        CallCancelled = RuntimeError  # type: ignore[misc, assignment]
        resolve_cancel_event = lambda event=None: event  # noqa: E731
        run_cancellable = None  # type: ignore[assignment]

    event = resolve_cancel_event(cancel_event)
    if event is not None and event.is_set():
        raise MechanismLLMError("LLM call cancelled")

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

    def _request_and_parse() -> str:
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
        return text

    try:
        if event is not None and run_cancellable is not None:
            text = run_cancellable(_request_and_parse, cancel_event=event, poll_sec=0.2)
        else:
            text = _request_and_parse()
    except CallCancelled as exc:
        raise MechanismLLMError("LLM call cancelled") from exc

    if settings.use_cache:
        _write_cache(settings.cache_dir, key, model=settings.model, content=text)
    return text


def chat_completion_stream(
    settings: LLMSettings,
    *,
    system: str,
    user: str,
    cancel_event: Any = None,
) -> Iterator[str]:
    """Stream OpenAI-compatible chat completion deltas (``choices[].delta.content``).

    Yields non-empty content chunks as they arrive. On cache hit (when enabled),
    yields the full cached string once. Callers that need the complete reply
    should join the yielded chunks.
    """
    if not settings.ready:
        raise MechanismLLMError("LLM 未就绪（未启用或缺少 API Key）")

    try:
        from agent.runtime.cancellable_call import (
            CallCancelled,
            resolve_cancel_event,
        )
    except Exception:  # noqa: BLE001 — keep mechanism usable outside agent runtime
        CallCancelled = RuntimeError  # type: ignore[misc, assignment]
        resolve_cancel_event = lambda event=None: event  # noqa: E731

    event = resolve_cancel_event(cancel_event)
    if event is not None and event.is_set():
        raise MechanismLLMError("LLM call cancelled")

    key = _cache_key(settings.model, system, user, settings.temperature)
    if settings.use_cache:
        cached = _read_cache(settings.cache_dir, key)
        if cached:
            yield cached
            return

    url = f"{settings.base_url}/chat/completions"
    payload = {
        "model": settings.model,
        "temperature": settings.temperature,
        "max_tokens": settings.max_tokens,
        "stream": True,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    pieces: list[str] = []
    saw_content = False
    saw_reasoning = False

    def _raise_if_cancelled() -> None:
        if event is not None and event.is_set():
            raise CallCancelled("LLM stream cancelled")

    def _consume_sse_line(raw_line: str) -> Iterator[str]:
        nonlocal saw_content, saw_reasoning
        raw = str(raw_line or "").strip()
        if not raw or raw.startswith(":"):
            return
        if not raw.startswith("data:"):
            return
        data = raw[5:].strip()
        if not data or data == "[DONE]":
            return
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as exc:
            raise MechanismLLMError(f"流式响应解析失败: {exc}") from exc
        try:
            delta = chunk["choices"][0].get("delta") or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise MechanismLLMError(f"流式响应缺少 delta: {exc}") from exc
        if delta.get("reasoning_content"):
            saw_reasoning = True
        content = delta.get("content")
        if not content:
            return
        piece = str(content)
        if not piece:
            return
        saw_content = True
        pieces.append(piece)
        yield piece

    try:
        timeout = httpx.Timeout(
            settings.timeout_sec,
            connect=min(12.0, settings.timeout_sec),
        )
        with httpx.Client(timeout=timeout) as client:
            with client.stream("POST", url, headers=headers, json=payload) as resp:
                try:
                    resp.raise_for_status()
                except httpx.HTTPError as exc:
                    # Drain error body so the connection closes cleanly.
                    try:
                        resp.read()
                    except Exception:  # noqa: BLE001
                        pass
                    raise MechanismLLMError(f"HTTP 失败: {exc}") from exc
                # Byte/text iteration avoids waiting for the full body before the
                # first yield (unlike some buffered line iterators behind proxies).
                pending = ""
                done = False
                for text_chunk in resp.iter_text():
                    _raise_if_cancelled()
                    if not text_chunk:
                        continue
                    pending += text_chunk
                    while "\n" in pending and not done:
                        line, pending = pending.split("\n", 1)
                        stripped = line.strip()
                        if stripped == "data: [DONE]" or stripped.endswith("[DONE]"):
                            done = True
                            break
                        yield from _consume_sse_line(line)
                    if done:
                        break
                if not done and pending.strip():
                    yield from _consume_sse_line(pending)
    except CallCancelled as exc:
        raise MechanismLLMError("LLM call cancelled") from exc
    except httpx.HTTPError as exc:
        raise MechanismLLMError(f"HTTP 失败: {exc}") from exc

    text = "".join(pieces).strip()
    if not text:
        if saw_reasoning and not saw_content:
            raise MechanismLLMError(
                "模型仅返回 reasoning_content、content 为空；"
                "请增大 llm.max_tokens（推理模型需预留 reasoning 额度）"
            )
        raise MechanismLLMError("模型返回空内容")

    if settings.use_cache:
        _write_cache(settings.cache_dir, key, model=settings.model, content=text)