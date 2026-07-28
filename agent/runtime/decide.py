"""对话决策：优先 LLM JSON 分类，无模型时走结构降级（不用确认词表）。"""

from __future__ import annotations

import json
import re
from typing import Any


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

    try:
        from plugins.molmind_core.scientific.mechanism.llm_client import (
            chat_completion,
            resolve_llm_settings,
        )

        settings = resolve_llm_settings(
            {"enabled": True, "agent_chat": True}, purpose=purpose
        )
        if not settings.ready:
            return fallback, "llm_not_ready"

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
        raw = chat_completion(settings, system=system, user=user).strip()
        m = re.search(r"\{[\s\S]*\}", raw)
        data: dict[str, Any] = json.loads(m.group(0) if m else raw)
        decision = str(data.get("decision") or fallback).strip().lower()
        if decision not in allowed_norm:
            decision = fallback
        reason = str(data.get("reason") or "llm").strip() or "llm"
        return decision, reason
    except Exception as exc:  # noqa: BLE001 — LLM optional
        return fallback, f"llm_unavailable:{type(exc).__name__}"
