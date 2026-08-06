"""Bounded, auditable context construction for chat and interrupted Runs."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContextWindow:
    history: str
    working_memory: str
    resume_context: str
    summary: dict[str, Any] | None


def _clip(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _compact_json(value: object, limit: int) -> str:
    text = json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)
    return _clip(text, limit)


def estimate_tokens(value: object) -> int:
    """Conservative local estimate for mixed Chinese, English and JSON text."""
    text = str(value or "")
    cjk = len(re.findall(r"[\u3400-\u9fff\uf900-\ufaff]", text))
    non_cjk = len(text) - cjk
    return cjk + math.ceil(non_cjk / 4)


def build_context_window(
    *,
    messages: list[dict[str, Any]],
    working_memory: list[dict[str, Any]],
    resume_context: dict[str, Any] | None,
    max_input_tokens: int = 6_000,
    reserved_tokens: int = 1_800,
) -> ContextWindow:
    """Keep exact recent turns and summarize older text under an approximate budget.

    Chinese and JSON tokenization varies by model. The budget reserves the
    system/current turn, while summary metadata records a mixed-text estimate.
    """
    char_budget = max(4_000, (max_input_tokens - reserved_tokens) * 4)
    history_budget = int(char_budget * 0.48)
    working_budget = int(char_budget * 0.22)
    resume_budget = int(char_budget * 0.30)

    usable = [
        item
        for item in messages
        if item.get("role") in {"user", "assistant"} and str(item.get("text") or "").strip()
    ]
    recent: list[str] = []
    used = 0
    split_at = len(usable)
    for index in range(len(usable) - 1, -1, -1):
        item = usable[index]
        line = f"{item.get('role')}: {_clip(item.get('text'), 1200)}"
        if recent and used + len(line) > history_budget:
            split_at = index + 1
            break
        recent.append(line)
        used += len(line)
        split_at = index
    recent.reverse()

    older = usable[:split_at]
    summary = None
    if older:
        summary_lines = [
            f"{item.get('role')}: {_clip(item.get('text'), 220)}"
            for item in older[-24:]
        ]
        summary_text = _clip("\n".join(summary_lines), max(800, history_budget // 3))
        source = json.dumps(older, ensure_ascii=False, sort_keys=True, default=str)
        summary = {
            "covered_messages": len(older),
            "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "text": summary_text,
            "source_estimated_tokens": estimate_tokens(source),
            "summary_estimated_tokens": estimate_tokens(summary_text),
            "reason": "input_budget_exceeded",
        }
        recent.insert(0, "较早对话压缩摘要：\n" + summary_text)

    return ContextWindow(
        history="\n".join(recent) or "（无）",
        working_memory=_compact_json(working_memory[-6:], working_budget) or "（无）",
        resume_context=_compact_json(resume_context or {}, resume_budget) or "（无）",
        summary=summary,
    )
