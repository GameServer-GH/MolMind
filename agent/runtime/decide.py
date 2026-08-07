"""对话决策：优先 LLM JSON 分类，无模型时走结构降级（不用确认词表）。"""

from __future__ import annotations

import json
import re
from typing import Any


def _chat_json_payload(
    *,
    system: str,
    user: str,
    purpose: str,
    max_tokens: int,
    timeout_sec: float,
    memory_cache: bool = True,
) -> dict[str, Any]:
    from plugins.molmind_core.scientific.mechanism.llm_client import (
        chat_completion,
        resolve_llm_settings,
    )

    settings = resolve_llm_settings(
        {"enabled": True, "agent_chat": True}, purpose=purpose
    )
    if not settings.ready:
        raise RuntimeError("llm_not_ready")

    settings = type(settings)(
        enabled=settings.enabled,
        model=settings.model,
        base_url=settings.base_url,
        api_key=settings.api_key,
        temperature=0.0,
        timeout_sec=min(settings.timeout_sec, timeout_sec),
        max_tokens=max_tokens,
        cache_dir=settings.cache_dir,
        use_cache=False,
    )
    raw = chat_completion(
        settings, system=system, user=user, memory_cache=memory_cache
    ).strip()
    match = re.search(r"\{[\s\S]*\}", raw)
    data = json.loads(match.group(0) if match else raw)
    if not isinstance(data, dict):
        raise ValueError("llm_json_not_object")
    return data


def _reraise_cancel(exc: BaseException) -> None:
    from agent.runtime.cancellable_call import CallCancelled
    from plugins.molmind_core.scientific.mechanism.llm_client import MechanismLLMError

    if isinstance(exc, CallCancelled):
        raise exc
    if isinstance(exc, MechanismLLMError) and "cancelled" in str(exc).lower():
        raise CallCancelled("llm cancelled") from exc


def llm_json_object(
    *,
    system: str,
    user: str,
    default: dict[str, Any],
    purpose: str = "agent_chat",
    max_tokens: int = 256,
    timeout_sec: float = 30.0,
    retries: int = 1,
    memory_cache: bool = True,
) -> tuple[dict[str, Any], str]:
    """Return (payload, status_tag).

    ``status_tag`` is ``ok`` on success, otherwise an ``llm_*`` failure tag.
    On failure returns a shallow copy of ``default``.
    Retries once on JSON parse errors (not on cancel / not-ready).
    """
    fallback = dict(default)
    attempts = max(1, int(retries) + 1)
    last_tag = "llm_unavailable"
    for attempt in range(attempts):
        try:
            data = _chat_json_payload(
                system=system,
                user=user,
                purpose=purpose,
                max_tokens=max_tokens,
                timeout_sec=timeout_sec,
                memory_cache=memory_cache,
            )
            return data, "ok"
        except Exception as exc:  # noqa: BLE001 — LLM optional
            _reraise_cancel(exc)
            if str(exc) == "llm_not_ready":
                return fallback, "llm_not_ready"
            last_tag = f"llm_unavailable:{type(exc).__name__}"
            # Retry only parse/shape failures; transport/model errors still fall back.
            if attempt + 1 >= attempts or type(exc).__name__ not in {
                "JSONDecodeError",
                "ValueError",
            }:
                break
    return fallback, last_tag


def llm_json_decision(
    *,
    system: str,
    user: str,
    allowed: set[str],
    default: str,
    purpose: str = "agent_chat",
    max_tokens: int = 256,
    timeout_sec: float = 30.0,
) -> tuple[str, str]:
    """Return (decision, reason). On failure returns (default, why)."""
    allowed_norm = {a.strip().lower() for a in allowed}
    fallback = default.strip().lower()
    if fallback not in allowed_norm:
        fallback = next(iter(allowed_norm), "other")

    data, status = llm_json_object(
        system=system,
        user=user,
        default={"decision": fallback, "reason": "offline"},
        purpose=purpose,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
    )
    if status != "ok":
        return fallback, status
    decision = str(data.get("decision") or fallback).strip().lower()
    if decision not in allowed_norm:
        decision = fallback
    reason = str(data.get("reason") or "llm").strip() or "llm"
    return decision, reason
