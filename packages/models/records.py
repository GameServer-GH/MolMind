"""领域数据模型（ADR-M16：禁止 SI/EC50/CC50 等数值字段进 CSV 契约）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal

# 禁止出现在可序列化/导出契约字段名中的词（ADR-M16）
FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "si",
        "ec50",
        "cc50",
        "selectivity_index",
        "viability_pct",
        "viability_percent",
    }
)


@dataclass
class MoleculeRecord:
    molecule_id: str
    smiles: str
    inchikey: str
    cas: str | None
    mw: float
    logp: float
    hbd: int
    hba: int
    tpsa: float
    rotatable_bonds: int
    aromatic_rings: int
    fp_bits: Any = None  # ExplicitBitVect | None
    source_index: int = 0
    source_molecule_id: str = ""
    original_smiles: str = ""
    standardization_steps: tuple[str, ...] = ()


ScreeningStatus = Literal["passed", "rejected", "review_required", "invalid"]
EligibilityStatus = Literal["eligible", "ineligible", "review_required"]
ScientificStatus = Literal[
    "task_specific_experiment_supported",
    "candidate_activity_evidence",
    "mechanism_support_only",
    "risk_evidence_only",
    "proxy_only",
    "identity_review_required",
]
EvidenceQueryStatus = Literal[
    "exact_hit",
    "analogue_hit",
    "annotation_only",
    "verified_empty",
    "identity_review_required",
    "timeout",
    "rate_limited",
    "adapter_error",
    "not_queried",
]
# 与 evidence_role/query_status 并存的导出词汇，便于审计对齐。
EvidenceType = Literal[
    "identity_annotation",
    "endpoint_evidence",
    "mechanism_context",
    "query_audit",
    "unresolved",
]


@dataclass(frozen=True)
class StructuralAlertHit:
    rule_id: str
    classification: str
    smarts: str


@dataclass(frozen=True)
class ParseIssue:
    source_index: int
    molecule_id: str
    status: ScreeningStatus
    reason_code: str
    reason: str


@dataclass
class FilterDecision:
    passed: bool
    step_codes: list[str] = field(default_factory=list)
    reason: str = ""
    status: ScreeningStatus = "passed"
    reason_codes: list[str] = field(default_factory=list)
    alert_hits: list[StructuralAlertHit] = field(default_factory=list)


@dataclass(frozen=True)
class ScreeningAuditRecord:
    molecule_id: str
    source_index: int
    status: ScreeningStatus
    reason_codes: tuple[str, ...]
    reason: str
    alert_hits: tuple[str, ...] = ()


@dataclass(frozen=True)
class MoleculeAssessment:
    molecule_id: str
    lipid_score: float
    toxicity_score: float
    toxicity_confidence: float
    toxicity_evidence_coverage: float | None = None
    safety_clearance_confidence: float | None = None
    toxicity_upper_bound: float | None = None


@dataclass(frozen=True)
class EligibilityPolicy:
    lipid_min: float
    tox_hard: float
    tox_nomination_max: float
    min_toxicity_confidence: float = 0.0
    min_safety_evidence_coverage: float = 0.0


@dataclass(frozen=True)
class EligibilityDecision:
    status: EligibilityStatus
    reasons: tuple[str, ...]
    lipid_pass: bool
    toxicity_pass: bool
    hard_toxicity_pass: bool
    confidence_pass: bool
    evidence_coverage_pass: bool = True

    @property
    def is_eligible(self) -> bool:
        return self.status == "eligible"


@dataclass
class Attribution:
    source: str
    detail: str
    value: float | None = None
    evidence_id: str | None = None


@dataclass
class EvidenceHit:
    adapter_id: str
    query_type: str  # lipid | tox | novelty | pathway
    score: float
    confidence: float
    evidence_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    endpoint: str = ""
    direction: str = "unknown"
    evidence_role: str = "task_evidence"
    provenance_status: str = "legacy"
    source_url: str = ""
    retrieved_at: str = ""
    adapter_version: str = ""
    source_version: str = ""
    query_params: dict[str, Any] = field(default_factory=dict)
    response_sha256: str = ""
    license: str = ""
    query_status: EvidenceQueryStatus = "not_queried"
    evidence_type: EvidenceType = "unresolved"


@dataclass
class EvidenceCitation:
    """候选级可追溯引用行；缺失字段保持空串，禁止捏造 PMID/DOI。"""

    source: str
    accession: str
    evidence_type: str
    endpoint: str
    direction: str
    value: str = ""
    unit: str = ""
    assay_context: str = ""
    matched_entity: str = ""
    pmid_or_doi: str = ""
    queried_at: str = ""
    evidence_id: str = ""


@dataclass
class ScoreRecord:
    molecule_id: str
    smiles: str
    inchikey: str
    cas: str | None
    scaffold_smiles: str
    lipid_score: float
    tox_risk: float
    novelty_score: float
    conf_e: float
    final_score: float
    tox_heads: dict[str, float]
    lipid_parts: dict[str, float]
    attributions: list[Attribution]
    lipid_rationale: str
    tox_rationale: str
    overall_reason: str
    toxicity_confidence: float = 0.0
    toxicity_uncertainty: float = 1.0
    eligibility_status: EligibilityStatus = "ineligible"
    eligibility_reasons: tuple[str, ...] = ()
    gated_out: bool = False
    gate_reason: str = ""
    fp_bits: Any = None
    lipid_evidence_confidence: float = 0.0
    toxicity_model_applicability: float = 0.0
    toxicity_evidence_coverage: float = 0.0
    risk_signal_confidence: float = 0.0
    safety_clearance_confidence: float = 0.0
    proxy_clearance_confidence: float = 0.0
    tox_upper_bound: float = 1.0
    novelty_reference_version: str = ""
    novelty_nearest_reference: str = ""
    novelty_max_similarity: float = 0.0
    internal_nearest_similarity: float = 0.0
    similarity_cluster: str = ""
    selection_tier: str = "score_only"
    selection_reason: str = ""
    selection_factors: dict[str, str] = field(default_factory=dict)
    robust_inclusion_frequency: float = 0.0
    robust_rank_median: float | None = None
    robust_rank_min: int | None = None
    robust_rank_max: int | None = None
    # 科学状态与计算资格相互独立：eligible 只表示项目代理门控通过。
    scientific_status: ScientificStatus = "proxy_only"
    claim_ceiling: str = "proxy_nomination"
    audit_missing: tuple[str, ...] = ("lipid_activity", "safety_clearance")
    lipid_evidence_status: EvidenceQueryStatus = "not_queried"
    toxicity_evidence_status: EvidenceQueryStatus = "not_queried"
    evidence_hits: list[EvidenceHit] = field(default_factory=list)
    citations: list[EvidenceCitation] = field(default_factory=list)
    evidence_run_id: str = ""
    input_structure_hash: str = ""
    epa_audit: dict[str, Any] = field(default_factory=dict)
    dili_audit: dict[str, Any] = field(default_factory=dict)
    evidence_source_audit: dict[str, Any] = field(default_factory=dict)
    # 评分代理：在同次运行的 eligible 池内做相对排序后赋值。
    effect_proxy_score: float = 0.0
    novelty_proxy_score: float = 0.0
    effect_rank: int | None = None
    novelty_rank: int | None = None
    effect_x_novelty: float = 0.0
    effect_novelty_equal_mean: float = 0.0
    selection_score: float = 0.0
    competition_scoring_version: str = "unassigned"
    screening_concentration_um: float = 10.0
    viability_endpoint: str = "CCK-8"
    viability_threshold_reference: str = ">0.80_relative_to_control"
    dual_endpoint_claim: str = "lipid_and_viability_parallel_required"
    nomination_tier: str = "unassigned"
    primary_rank: int | None = None
    reserve_rank: int | None = None
    replacement_for: str = ""
    purchase_status: str = "unknown"
    solubility_status: str = "unknown"
    identity_status: str = "resolved"


@dataclass
class CriticAction:
    action: str  # keep | drop | replace
    molecule_id: str
    reason: str
    evidence_ids: list[str] = field(default_factory=list)
    replacement_id: str | None = None
    original_status: str = ""
    checks_performed: tuple[str, ...] = ()
    score_before: float | None = None
    score_after: float | None = None
    eligibility_before: str = ""
    eligibility_after: str = ""
    rank_before: int | None = None
    rank_after: int | None = None
    final_decision: str = ""


@dataclass
class RunDiagnostics:
    input_count: int
    filtered_out: int
    eligible_count: int
    ro5_pass_rate: float
    std_tox: float
    scaffold_diversity_top10: int
    evidence_coverage_ratio: float
    goldset_fp_in_top10: int
    goldset_pos_percentile: float | None
    config_hash: str
    mode: str
    degraded_channels: list[str]
    quality_pass: bool
    notes: list[str] = field(default_factory=list)
    raw_count: int = 0
    parse_skipped: int = 0
    inchikey_missing: int = 0
    engineering_pass: bool = True
    model_coverage_status: str = "unknown"
    scientific_validation_status: str = "not_available"


def format_selection_reason(factors: dict[str, str]) -> str:
    """Deterministic CSV-compatible summary of structured selection factors."""
    order = (
        "eligibility",
        "score",
        "scaffold_diversity",
        "evidence_coverage",
        "combo_adjustment",
    )
    parts = [f"{key}={factors[key]}" for key in order if factors.get(key)]
    extras = [f"{k}={v}" for k, v in sorted(factors.items()) if k not in order and v]
    return "; ".join(parts + extras)


def serialize_record(obj: Any) -> dict[str, Any]:
    """Dataclass → dict；跳过 fp_bits 等不可 JSON 化字段。"""
    data = asdict(obj)
    data.pop("fp_bits", None)
    return data


def assert_no_forbidden_fields(cls: type) -> None:
    names = {f.name.lower() for f in fields(cls)}
    bad = names & FORBIDDEN_FIELD_NAMES
    if bad:
        raise AssertionError(f"{cls.__name__} 含 ADR-M16 禁止字段: {sorted(bad)}")
