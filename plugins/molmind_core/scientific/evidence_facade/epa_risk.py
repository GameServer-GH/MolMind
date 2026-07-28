"""EPA ToxCast cytotox risk tiers for stage-2 scoring.

Active assay counts alone are bioactivity annotations, not toxicity scores.
Risk scoring uses nhit together with cytotoxLowerUm vs the fixed 10 µM screen.
"""

from __future__ import annotations

from typing import Any


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def epa_cytotox_metrics(entry: dict[str, Any] | None) -> dict[str, Any]:
    row = dict(entry or {})
    nhit = _float_or_none(row.get("nhit"))
    if nhit is None:
        nhit = 0.0
    return {
        "nhit": nhit,
        "cytotox_lower_um": _float_or_none(row.get("cytotox_lower_um") or row.get("cytotoxLowerUm")),
        "cytotox_median_um": _float_or_none(
            row.get("cytotox_median_um") or row.get("cytotoxMedianUm")
        ),
        "active_hit_count": int(row.get("active_hit_count") or 0),
    }


def epa_cytotox_risk_tier(
    entry: dict[str, Any] | None,
    *,
    screening_um: float = 10.0,
) -> str:
    """Classify EPA summary for ranking.

    Returns one of:
    - ``strong_risk``: nhit>0 and cytotoxLowerUm ≤ screening_um
    - ``weak_risk_review``: nhit>0 but lower bound above screening_um
    - ``bioactivity_annotation``: active assays without cytotox hits
    - ``none``: no usable bioactivity/cytotox signal
    """
    metrics = epa_cytotox_metrics(entry)
    nhit = float(metrics["nhit"])
    lower = metrics["cytotox_lower_um"]
    active = int(metrics["active_hit_count"])
    if nhit > 0:
        if lower is not None and lower <= float(screening_um):
            return "strong_risk"
        return "weak_risk_review"
    if active > 0:
        return "bioactivity_annotation"
    return "none"


def risk_tier_rank(tier: str) -> int:
    return {
        "strong_risk": 3,
        "weak_risk_review": 2,
        "bioactivity_annotation": 1,
        "none": 0,
    }.get(str(tier or "none"), 0)
