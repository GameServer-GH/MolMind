"""EvidenceBundle：lipid/tox/novelty/pathway 证据聚合。"""

from __future__ import annotations

from dataclasses import dataclass, field

from packages.models import EvidenceHit


_SCORING_QUERY_STATUSES = frozenset({"hit", "exact_hit", "analogue_hit"})


def _is_scientific_hit(hit: EvidenceHit) -> bool:
    """Return true only for an explicit, normalized scientific hit."""

    return str(hit.query_status) in _SCORING_QUERY_STATUSES


def infer_evidence_type(hit: EvidenceHit) -> str:
    """Map internal role/status onto export evidence_type vocabulary."""
    if hit.evidence_type and hit.evidence_type != "unresolved":
        return hit.evidence_type
    if hit.evidence_role == "query_audit" or hit.query_type == "query_audit":
        return "query_audit"
    if hit.evidence_role == "annotation_only" or hit.query_status == "annotation_only":
        return "identity_annotation"
    if hit.evidence_role == "mechanism_support" or hit.query_type == "pathway":
        return "mechanism_context"
    if hit.evidence_role == "task_evidence" and hit.query_type in {"lipid", "tox"}:
        return "endpoint_evidence"
    if hit.query_status == "identity_review_required":
        return "identity_annotation"
    return "unresolved"


@dataclass
class EvidenceBundle:
    lipid: list[EvidenceHit] = field(default_factory=list)
    tox: list[EvidenceHit] = field(default_factory=list)
    novelty: list[EvidenceHit] = field(default_factory=list)
    pathway: list[EvidenceHit] = field(default_factory=list)
    annotation: list[EvidenceHit] = field(default_factory=list)
    query_audit: list[EvidenceHit] = field(default_factory=list)
    run_id: str = ""
    input_structure_hash: str = ""
    normalized_inchikey: str = ""
    queried_at: str = ""
    source_versions: dict[str, str] = field(default_factory=dict)
    # EPA stage/audit payload is report-visible even when stage 1 is
    # non-scoring.  Keep it separate from lipid/tox task evidence so query
    # status cannot be misread as efficacy or safety clearance.
    epa_audit: dict[str, object] = field(default_factory=dict)
    # DILIrank exact-identity audit / hard-exclude flag (never safety clearance).
    dili_audit: dict[str, object] = field(default_factory=dict)
    # Per-source shortlist query summary for PDF/CSV (ChEMBL/PubChem/BindingDB).
    evidence_source_audit: dict[str, object] = field(default_factory=dict)
    # Query-only gateway metadata.  These fields are deliberately appended so
    # historical positional construction stays compatible, and kept separate
    # from evidence channels consumed by the deterministic scientific scorer.
    identity: dict[str, object] = field(default_factory=dict)
    query_plan: list[dict[str, object]] = field(default_factory=list)
    degraded_channels: list[str] = field(default_factory=list)

    @property
    def conf_e(self) -> float:
        """与降脂终点相关的证据置信度；毒性/新颖性/查询状态不得串线加分。"""
        if self.has_identity_review_required:
            return 0.0
        hits = [
            hit
            for hit in self.lipid
            if hit.evidence_role == "task_evidence"
            and hit.query_type == "lipid"
            and hit.direction not in {"contradicts", "risk", "negative"}
            and _is_scientific_hit(hit)
        ]
        if not hits:
            return 0.0
        return sum(h.confidence for h in hits) / len(hits)

    @property
    def lipid_evidence_confidence(self) -> float:
        return self.conf_e

    @property
    def toxicity_evidence_coverage(self) -> float:
        hits = [
            hit
            for hit in self.tox
            if hit.evidence_role == "task_evidence"
            and hit.query_type == "tox"
            and _is_scientific_hit(hit)
        ]
        if self.has_identity_review_required:
            # Identity ambiguity may conservatively retain an adverse signal,
            # but it can never manufacture safety-clearance coverage.
            hits = [
                hit
                for hit in hits
                if hit.direction in {"risk", "contradicts", "adverse", "negative"}
            ]
        return max((h.confidence for h in hits), default=0.0)

    @property
    def lipid_score(self) -> float:
        if self.has_identity_review_required:
            return 0.0
        hits = [
            hit
            for hit in self.lipid
            if hit.evidence_role == "task_evidence"
            and hit.query_type == "lipid"
            and hit.direction not in {"contradicts", "risk", "negative"}
            and _is_scientific_hit(hit)
        ]
        return max((hit.score for hit in hits), default=0.0)

    @property
    def tox_score(self) -> float:
        hits = [
            hit
            for hit in self.tox
            if hit.evidence_role in {"task_evidence", "risk_signal"}
            and hit.query_type == "tox"
            and _is_scientific_hit(hit)
        ]
        if self.has_identity_review_required:
            # Existing project policy allows only conservative risk
            # propagation across an explicitly audited identity ambiguity.
            hits = [
                hit
                for hit in hits
                if hit.direction in {"risk", "contradicts", "adverse", "negative"}
            ]
        return max((hit.score for hit in hits), default=0.0)

    @property
    def novelty_score(self) -> float:
        if self.has_identity_review_required:
            return 0.0
        hits = [
            hit
            for hit in self.novelty
            if hit.evidence_role == "task_evidence"
            and hit.query_type == "novelty"
            and _is_scientific_hit(hit)
        ]
        return max((hit.score for hit in hits), default=0.0)

    @property
    def has_any(self) -> bool:
        # Annotation/query-audit rows (including EPA stage 1) are report
        # material, not endpoint hits.  Keep this property as the historical
        # "candidate has scoring-relevant evidence" signal.
        return any(
            hit.evidence_role in {"task_evidence", "risk_signal", "mechanism_support"}
            and hit.query_type != "query_audit"
            and _is_scientific_hit(hit)
            for hit in [*self.lipid, *self.tox, *self.novelty, *self.pathway]
        )

    def all_ids(self) -> list[str]:
        return [
            h.evidence_id
            for h in [
                *self.lipid,
                *self.tox,
                *self.novelty,
                *self.pathway,
                *self.annotation,
                *self.query_audit,
            ]
        ]

    def all_hits(self) -> list[EvidenceHit]:
        return [
            *self.lipid,
            *self.tox,
            *self.novelty,
            *self.pathway,
            *self.annotation,
            *self.query_audit,
        ]

    def annotate_evidence_types(self) -> None:
        """Fill evidence_type on every hit for export / export vocabulary alignment."""
        for hit in self.all_hits():
            hit.evidence_type = infer_evidence_type(hit)  # type: ignore[assignment]
            if not hit.source_version:
                hit.source_version = hit.adapter_version or hit.adapter_id

    def collect_source_versions(self) -> dict[str, str]:
        versions: dict[str, str] = {}
        for hit in self.all_hits():
            version = hit.source_version or hit.adapter_version
            if hit.adapter_id and version:
                versions[hit.adapter_id] = version
        return versions

    @staticmethod
    def _best_query_status(hits: list[EvidenceHit]) -> str:
        priority = {
            # Identity ambiguity is a gate, never something a provider hit can
            # hide in a summary status.
            "identity_review_required": 100,
            "hit": 9,
            "exact_hit": 8,
            "analogue_hit": 7,
            "annotation_only": 5,
            "verified_empty": 4,
            "auth_missing": 3,
            "query_failed": 2,
            "rate_limited": 3,
            "timeout": 2,
            "adapter_error": 1,
            "not_queried": 0,
        }
        return max(
            (hit.query_status for hit in hits),
            key=lambda status: priority.get(status, -1),
            default="not_queried",
        )

    @property
    def lipid_query_status(self) -> str:
        # 仅脂质终点通道；ChEMBL annotation_only 不得伪装成 lipid 查询命中。
        related = [hit for hit in self.lipid if hit.query_type == "lipid"]
        return self._best_query_status(related)

    @property
    def toxicity_query_status(self) -> str:
        related = [hit for hit in self.tox if hit.query_type == "tox"]
        return self._best_query_status(related)

    @property
    def has_safety_clearance_evidence(self) -> bool:
        if self.has_identity_review_required:
            return False
        return any(
            hit.evidence_role == "task_evidence"
            and hit.query_type == "tox"
            and hit.direction in {"supports_safety", "low_risk"}
            and _is_scientific_hit(hit)
            for hit in self.tox
        )

    @property
    def has_identity_review_required(self) -> bool:
        return any(
            hit.query_status == "identity_review_required" for hit in self.all_hits()
        )
