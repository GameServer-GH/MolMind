"""声明天花板等策略（轻量）。"""

from __future__ import annotations

FORBIDDEN_CLAIM_FIELDS = ("SI", "EC50", "CC50", "ec50", "cc50")


def claim_ceiling_default() -> str:
    return "proxy_priority_only"


def assert_no_forbidden_fields(payload: dict) -> None:
    bad = [k for k in payload if k in FORBIDDEN_CLAIM_FIELDS]
    if bad:
        raise ValueError(f"禁止伪精确字段: {bad}")
