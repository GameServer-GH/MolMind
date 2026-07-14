"""领域数据模型（ADR-M16：禁止 SI/EC50/CC50 等数值字段进 CSV 契约）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

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


@dataclass
class FilterDecision:
    passed: bool
    step_codes: list[str] = field(default_factory=list)
    reason: str = ""


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
    gated_out: bool = False
    gate_reason: str = ""
    fp_bits: Any = None


@dataclass
class CriticAction:
    action: str  # keep | drop | replace
    molecule_id: str
    reason: str
    evidence_ids: list[str] = field(default_factory=list)
    replacement_id: str | None = None


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
