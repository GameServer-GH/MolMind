"""EvidenceBundle：lipid/tox/novelty/pathway 证据聚合。"""

from __future__ import annotations

from dataclasses import dataclass, field

from packages.models import EvidenceHit


def infer_evidence_type(hit: EvidenceHit) -> str:
    """Map internal role/status onto OriGene evidence_type vocabulary."""
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

    @property
    def conf_e(self) -> float:
        """与降脂终点相关的证据置信度；毒性/新颖性/查询状态不得串线加分。"""
        hits = [
            hit
            for hit in self.lipid
            if hit.evidence_role == "task_evidence"
            and hit.direction not in {"contradicts", "risk", "negative"}
        ]
        if not hits:
            return 0.0
        return sum(h.confidence for h in hits) / len(hits)

    @property
    def lipid_evidence_confidence(self) -> float:
        return self.conf_e

    @property
    def toxicity_evidence_coverage(self) -> float:
        hits = [h for h in self.tox if h.evidence_role == "task_evidence"]
        return max((h.confidence for h in hits), default=0.0)

    @property
    def lipid_score(self) -> float:
        if not self.lipid:
            return 0.0
        return max(h.score for h in self.lipid)

    @property
    def tox_score(self) -> float:
        if not self.tox:
            return 0.0
        return max(h.score for h in self.tox)

    @property
    def novelty_score(self) -> float:
        if not self.novelty:
            return 0.0
        return max(h.score for h in self.novelty)

    @property
    def has_any(self) -> bool:
        return bool(
            self.lipid
            or self.tox
            or self.novelty
            or self.pathway
            or self.annotation
            or self.query_audit
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
        """Fill evidence_type on every hit for export / OriGene vocabulary alignment."""
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
            "exact_hit": 8,
            "analogue_hit": 7,
            "identity_review_required": 6,
            "annotation_only": 5,
            "verified_empty": 4,
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
        return any(
            hit.evidence_role == "task_evidence"
            and hit.direction in {"supports_safety", "low_risk"}
            for hit in self.tox
        )

    @property
    def has_identity_review_required(self) -> bool:
        return any(
            hit.query_status == "identity_review_required" for hit in self.all_hits()
        )
