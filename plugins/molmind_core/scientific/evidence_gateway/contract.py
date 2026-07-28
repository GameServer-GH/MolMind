"""Canonical evidence gateway transport and normalization primitives.

Legacy statuses are accepted only at provider/snapshot input boundaries.  All
gateway, cache, Tool and Agent-facing output uses ``CANONICAL_STATUSES``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict
from typing import Any, Iterable, Mapping

from packages.models import EvidenceHit

NORMALIZED_EVIDENCE_SCHEMA_VERSION = "molmind-normalized-evidence-v3"
IDENTITY_RESOLVER_VERSION = "molmind-identity-resolver-v2"
CLASSIFICATION_RULES_VERSION = "molmind-evidence-classification-v2"

CANONICAL_STATUSES = frozenset(
    {
        "hit",
        "verified_empty",
        "query_failed",
        "auth_missing",
        "not_queried",
        "identity_review_required",
        "annotation_only",
    }
)
STATUS_ALIASES = {
    "exact_hit": "hit",
    "analogue_hit": "hit",
    "timeout": "query_failed",
    "rate_limited": "query_failed",
    "adapter_error": "query_failed",
    "network_error": "query_failed",
    "offline_missing": "not_queried",
    "audit_missing": "not_queried",
}
STATUS_PRIORITY = {
    "identity_review_required": 100,
    "hit": 80,
    "annotation_only": 70,
    # Current transport failures must not be hidden by an older empty row.
    "auth_missing": 66,
    "query_failed": 65,
    "verified_empty": 60,
    "not_queried": 10,
}

_SENSITIVE_KEY_RE = re.compile(
    r"(?:authorization|api[-_]?key|(?:access[-_]?|refresh[-_]?)?token|"
    r"credential|client[-_]?secret|private[-_]?key|password|secret)",
    re.I,
)
_SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+"),
    re.compile(
        r"(?i)((?:x-)?api[-_]?key|access[-_]?token|refresh[-_]?token|credential|"
        r"client[-_]?secret|private[-_]?key|password|secret)"
        r"(\s*[:=]\s*)[^\s&,;]+"
    ),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)([?&](?:key|api[-_]?key|token|access[-_]?token|refresh[-_]?token|"
        r"credential|client[-_]?secret|private[-_]?key)=)[^&#\s]+"
    ),
)


def redact_text(value: Any) -> str:
    text = str(value or "")
    for pattern in _SENSITIVE_TEXT_PATTERNS:
        replacement = r"\1[REDACTED]" if pattern.groups == 1 else r"\1\2[REDACTED]"
        text = pattern.sub(replacement, text)
    return text


def json_safe(value: Any) -> Any:
    """Return a redacted, deterministic JSON-safe value.

    Unknown objects are rendered as redacted strings. NaN and infinities are
    rejected rather than silently serialized into non-standard JSON.
    """

    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if _SENSITIVE_KEY_RE.search(str(key))
                else json_safe(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        return value
    if isinstance(value, str):
        return redact_text(value)
    return redact_text(value)


def content_sha256(value: Any) -> str:
    raw = json.dumps(
        json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def canonical_status(value: object, hit: EvidenceHit | None = None) -> str:
    raw = str(value or "").strip()
    status = STATUS_ALIASES.get(raw, raw)
    if status in CANONICAL_STATUSES:
        return status
    if hit is not None and hit.evidence_role == "annotation_only":
        return "annotation_only"
    # Unknown input fails closed. Query audits without a valid state are still
    # contract failures, not silently downgraded to "not queried".
    return "query_failed"


def aggregate_status(hits: Iterable[EvidenceHit]) -> str:
    values: list[str] = []
    for hit in hits:
        status = canonical_status(hit.query_status, hit)
        if (
            status == "hit"
            and hit.evidence_role not in {"query_audit", "annotation_only"}
        ):
            values.append("hit")
        else:
            values.append(status)
    if not values:
        return "verified_empty"
    return max(values, key=lambda item: STATUS_PRIORITY.get(item, -1))


def lookup_field_family(value: str) -> str:
    field = str(value or "").strip().lower()
    if field in {"inchikey", "original_inchikey", "standardized_inchikey"}:
        return "inchikey"
    if field in {"smiles", "canonical_smiles", "standardized_smiles"}:
        return "smiles"
    return field


def lookup_value_equal(field: str, left: str, right: str) -> bool:
    if lookup_field_family(field) == "inchikey":
        return str(left or "").strip().upper() == str(right or "").strip().upper()
    return str(left or "").strip() == str(right or "").strip()


def claim_ceiling(hit: EvidenceHit) -> str:
    if hit.claim_ceiling:
        return hit.claim_ceiling
    status = canonical_status(hit.query_status, hit)
    if status != "hit" or hit.evidence_role == "query_audit":
        return "query_transport_or_identity_audit_only_no_biological_conclusion"
    if hit.evidence_role == "annotation_only":
        return "database_annotation_only_not_efficacy_or_safety_evidence"
    if hit.evidence_role == "mechanism_support":
        return "mechanism_context_only_not_candidate_efficacy"
    if hit.query_type == "tox" and hit.direction == "risk":
        return "candidate_risk_signal_only_not_safety_clearance"
    return "candidate_preclinical_evidence_only_not_clinical_efficacy_or_safety"


def hit_asdict(hit: EvidenceHit) -> dict[str, Any]:
    return json_safe(asdict(hit))


__all__ = [
    "CANONICAL_STATUSES",
    "CLASSIFICATION_RULES_VERSION",
    "IDENTITY_RESOLVER_VERSION",
    "NORMALIZED_EVIDENCE_SCHEMA_VERSION",
    "STATUS_ALIASES",
    "STATUS_PRIORITY",
    "aggregate_status",
    "canonical_status",
    "claim_ceiling",
    "content_sha256",
    "hit_asdict",
    "json_safe",
    "lookup_field_family",
    "lookup_value_equal",
    "redact_text",
]
