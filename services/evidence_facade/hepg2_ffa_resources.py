"""Validated, non-scoring registry for public HepG2-FFA context resources.

Proteomics and Raman resources can support mechanism hypotheses and assay QC,
but they are never promoted to candidate-level lipid/viability labels.  A
separate record-level gate protects the optional dual-endpoint model interface.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from services.pipeline.config_loader import ROOT


REGISTRY_PATH = (
    ROOT / "data" / "evidence_snapshot" / "v2" / "hepg2_ffa_resources_v1.json"
)
REGISTRY_SCHEMA = "molmind-hepg2-ffa-resource-registry-v1"
RUNTIME_SCHEMA = "molmind-hepg2-ffa-resource-context-v1"
ALLOWED_ROLES = frozenset({"mechanistic_context", "assay_qc", "candidate_evidence_curation"})


class HepG2FFAResourceError(ValueError):
    """Raised when a frozen HepG2-FFA resource registry violates its contract."""


@dataclass(frozen=True)
class TrainingEligibilityDecision:
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class HepG2FFAResource:
    resource_id: str
    canonical_accession: str
    source_type: str
    title: str
    source_url: str
    role: str
    evidence_level: str
    training_eligible: bool
    ineligibility_reasons: tuple[str, ...]
    assay_context: Mapping[str, Any]
    raw: Mapping[str, Any]

    def to_runtime_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "canonical_accession": self.canonical_accession,
            "source_type": self.source_type,
            "title": self.title,
            "source_url": self.source_url,
            "role": self.role,
            "evidence_level": self.evidence_level,
            "training_eligible": self.training_eligible,
            "ineligibility_reasons": list(self.ineligibility_reasons),
            "assay_context": dict(self.assay_context),
        }


@dataclass(frozen=True)
class HepG2FFAResourceRegistry:
    schema_version: str
    captured_at: str
    ranking_effect: str
    scope: str
    training_gate: Mapping[str, Any]
    aliases: Mapping[str, str]
    resources: tuple[HepG2FFAResource, ...]
    literature_dual_endpoint: Mapping[str, Any]
    snapshot_sha256: str

    def canonicalize_accession(self, accession: str) -> str:
        return str(self.aliases.get(accession, accession))

    @property
    def mechanistic_context_count(self) -> int:
        return sum(item.role == "mechanistic_context" for item in self.resources)

    @property
    def assay_qc_count(self) -> int:
        return sum(item.role == "assay_qc" for item in self.resources)

    @property
    def candidate_dual_endpoint_resource_count(self) -> int:
        return sum(item.training_eligible for item in self.resources)

    @property
    def scientific_validation_status(self) -> str:
        return str(
            self.training_gate.get("current_status")
            or "no_validated_independent_dual_endpoint_benchmark"
        )

    def to_runtime_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RUNTIME_SCHEMA,
            "registry_schema_version": self.schema_version,
            "registry_sha256": self.snapshot_sha256,
            "captured_at": self.captured_at,
            "ranking_effect": self.ranking_effect,
            "scope": self.scope,
            "resource_counts": {
                "total": len(self.resources),
                "mechanistic_context": self.mechanistic_context_count,
                "assay_qc": self.assay_qc_count,
                "candidate_dual_endpoint_training_eligible": self.candidate_dual_endpoint_resource_count,
            },
            "scientific_validation_status": self.scientific_validation_status,
            "dual_endpoint_model_available": self.candidate_dual_endpoint_resource_count > 0,
            "aliases": dict(sorted(self.aliases.items())),
            "resources": [item.to_runtime_dict() for item in self.resources],
        }


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _require_text(item: Mapping[str, Any], field: str, *, resource_id: str) -> str:
    value = str(item.get(field) or "").strip()
    if not value:
        raise HepG2FFAResourceError(f"{resource_id}: missing {field}")
    return value


def _parse_resource(item: Mapping[str, Any]) -> HepG2FFAResource:
    resource_id = _require_text(item, "resource_id", resource_id="resource")
    role = _require_text(item, "role", resource_id=resource_id)
    if role not in ALLOWED_ROLES:
        raise HepG2FFAResourceError(f"{resource_id}: unsupported role={role}")
    training_eligible = item.get("training_eligible")
    if not isinstance(training_eligible, bool):
        raise HepG2FFAResourceError(f"{resource_id}: training_eligible must be boolean")
    reasons = tuple(str(value) for value in item.get("ineligibility_reasons") or [])
    if not training_eligible and not reasons:
        raise HepG2FFAResourceError(
            f"{resource_id}: ineligible resources require machine-readable reasons"
        )
    if training_eligible and role != "candidate_evidence_curation":
        raise HepG2FFAResourceError(
            f"{resource_id}: {role} resources cannot train the candidate endpoint model"
        )
    context = item.get("assay_context") or {}
    if not isinstance(context, Mapping):
        raise HepG2FFAResourceError(f"{resource_id}: assay_context must be an object")
    return HepG2FFAResource(
        resource_id=resource_id,
        canonical_accession=_require_text(item, "canonical_accession", resource_id=resource_id),
        source_type=_require_text(item, "source_type", resource_id=resource_id),
        title=_require_text(item, "title", resource_id=resource_id),
        source_url=_require_text(item, "source_url", resource_id=resource_id),
        role=role,
        evidence_level=_require_text(item, "evidence_level", resource_id=resource_id),
        training_eligible=training_eligible,
        ineligibility_reasons=reasons,
        assay_context=dict(context),
        raw=dict(item),
    )


def load_hepg2_ffa_resource_registry(
    path: Path | None = None,
) -> HepG2FFAResourceRegistry:
    registry_path = path or REGISTRY_PATH
    raw_bytes = registry_path.read_bytes()
    try:
        payload = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise HepG2FFAResourceError(f"invalid registry JSON: {exc}") from exc
    if payload.get("schema_version") != REGISTRY_SCHEMA:
        raise HepG2FFAResourceError(
            f"unsupported registry schema={payload.get('schema_version')!r}"
        )
    if payload.get("ranking_effect") != "none":
        raise HepG2FFAResourceError("public context resources must have ranking_effect=none")
    resources = tuple(_parse_resource(item) for item in payload.get("resources") or [])
    if not resources:
        raise HepG2FFAResourceError("resource registry is empty")
    resource_ids = [item.resource_id for item in resources]
    accessions = [item.canonical_accession for item in resources]
    if len(resource_ids) != len(set(resource_ids)):
        raise HepG2FFAResourceError("duplicate resource_id")
    if len(accessions) != len(set(accessions)):
        raise HepG2FFAResourceError("duplicate canonical_accession")
    aliases = payload.get("aliases") or {}
    if not isinstance(aliases, Mapping):
        raise HepG2FFAResourceError("aliases must be an object")
    known_accessions = set(accessions)
    for alias, target in aliases.items():
        if alias in known_accessions:
            raise HepG2FFAResourceError(f"alias shadows canonical accession: {alias}")
        if target not in known_accessions:
            raise HepG2FFAResourceError(f"alias target is unknown: {alias}->{target}")
    gate = payload.get("training_gate") or {}
    required_fields = gate.get("required_fields") or []
    if not required_fields or len(required_fields) != len(set(required_fields)):
        raise HepG2FFAResourceError("training_gate.required_fields must be unique and non-empty")
    if gate.get("requires_same_condition_paired_endpoints") is not True:
        raise HepG2FFAResourceError("same-condition lipid/viability pairing is mandatory")
    return HepG2FFAResourceRegistry(
        schema_version=payload["schema_version"],
        captured_at=str(payload.get("captured_at") or ""),
        ranking_effect="none",
        scope=str(payload.get("scope") or ""),
        training_gate=dict(gate),
        aliases={str(k): str(v) for k, v in aliases.items()},
        resources=resources,
        literature_dual_endpoint=dict(payload.get("literature_dual_endpoint") or {}),
        snapshot_sha256=_sha256_bytes(raw_bytes),
    )


def evaluate_dual_endpoint_training_record(
    record: Mapping[str, Any],
    registry: HepG2FFAResourceRegistry | None = None,
) -> TrainingEligibilityDecision:
    """Require a candidate structure and same-condition paired experimental endpoints."""
    active = registry or load_hepg2_ffa_resource_registry()
    missing = tuple(
        str(field)
        for field in active.training_gate.get("required_fields") or []
        if record.get(str(field)) in (None, "")
    )
    reasons: list[str] = []
    if missing:
        reasons.append("missing_required_fields:" + ",".join(missing))
    lipid_condition = record.get("lipid_condition_id")
    viability_condition = record.get("viability_condition_id")
    if lipid_condition != viability_condition or lipid_condition in (None, ""):
        reasons.append("lipid_viability_conditions_not_identical")
    source_id = str(record.get("source_id") or "")
    if not source_id:
        reasons.append("source_id_missing")
    return TrainingEligibilityDecision(eligible=not reasons, reasons=tuple(reasons))


def resource_registry_runtime_payload(
    registry: HepG2FFAResourceRegistry | None = None,
) -> dict[str, Any]:
    return (registry or load_hepg2_ffa_resource_registry()).to_runtime_dict()


__all__ = [
    "HepG2FFAResource",
    "HepG2FFAResourceError",
    "HepG2FFAResourceRegistry",
    "REGISTRY_PATH",
    "TrainingEligibilityDecision",
    "evaluate_dual_endpoint_training_record",
    "load_hepg2_ffa_resource_registry",
    "resource_registry_runtime_payload",
]
