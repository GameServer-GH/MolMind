"""降脂打分：规则 ∪ 阳性相似 ∪ 证据 ∪ 可选 ML。"""

from __future__ import annotations

from rdkit import Chem

from packages.chem_core import (
    LIPID_PATTERNS,
    PATHWAY_PATTERNS,
    clamp,
    lipid_descriptor_bonus,
    match_weighted,
)
from packages.goldset import GoldSet, max_similarity
from packages.models import Attribution, MoleculeRecord
from plugins.molmind_core.scientific.evidence_facade.bundle import EvidenceBundle
from plugins.molmind_core.scientific.pipeline.config_loader import AppConfig


def score_lipid(
    record: MoleculeRecord,
    cfg: AppConfig,
    gold: GoldSet,
    evidence: EvidenceBundle,
    *,
    positive_similarity: tuple[float, str | None] | None = None,
) -> tuple[float, dict[str, float], list[Attribution], str]:
    mol = Chem.MolFromSmiles(record.smiles)
    attrs: list[Attribution] = []
    if mol is None:
        return 0.0, {}, [Attribution("error", "SMILES 无效")], "SMILES 无效，降脂分置 0"

    pharma, hits = match_weighted(mol, LIPID_PATTERNS)
    pathway, pathway_hits = match_weighted(mol, PATHWAY_PATTERNS)
    desc_bonus, desc_notes = lipid_descriptor_bonus(record.logp, record.tpsa, record.rotatable_bonds)
    s_rule = clamp(0.15 + pharma + 0.5 * pathway + desc_bonus)
    for h in hits:
        attrs.append(Attribution("pharmacophore", h, value=None))
    for h in pathway_hits:
        attrs.append(Attribution("pathway_hint", h, value=None))

    if positive_similarity is None:
        pos_sim, pos_name = max_similarity(record.fp_bits, gold.positives)
    else:
        pos_sim, pos_name = positive_similarity
    s_sim = clamp(pos_sim)
    if pos_name:
        attrs.append(Attribution("positive_similarity", f"vs {pos_name}", value=round(pos_sim, 4)))

    s_evidence = clamp(evidence.lipid_score)
    for hit in evidence.lipid:
        attrs.append(
            Attribution(
                "evidence",
                hit.adapter_id,
                value=hit.score,
                evidence_id=hit.evidence_id,
            )
        )
    for hit in evidence.pathway:
        attrs.append(
            Attribution(
                "pathway_evidence",
                hit.adapter_id,
                value=hit.score,
                evidence_id=hit.evidence_id,
            )
        )

    # lipid ML 头未接线：s_ml 恒 0。仅当配置仍期望 ml 权重>0 且 ml 总开关关闭时记降级。
    s_ml = 0.0
    fuse = cfg.lipid_fuse
    ml_weight = float(fuse.get("ml", 0.0) or 0.0)
    if ml_weight > 0 and not cfg.ml_enabled:
        cfg.mark_degraded("lipid_ml_missing")

    active = {
        "rule": fuse["rule"],
        "positive_similarity": fuse["positive_similarity"],
        "evidence": fuse["evidence"] if s_evidence > 0 else 0.0,
        "ml": ml_weight if s_ml > 0 else 0.0,
    }
    weight_sum = sum(active.values()) or 1.0
    parts = {
        "rule": s_rule,
        "positive_similarity": s_sim,
        "evidence": s_evidence,
        "ml": s_ml,
    }
    s_lipid = clamp(
        (
            active["rule"] * s_rule
            + active["positive_similarity"] * s_sim
            + active["evidence"] * s_evidence
            + active["ml"] * s_ml
        )
        / weight_sum
    )

    rationale_parts = ["多信号降脂融合"]
    if hits:
        rationale_parts.append(f"药效团: {', '.join(hits)}")
    if pathway_hits:
        rationale_parts.append(f"通路启发: {', '.join(pathway_hits)}")
    rationale_parts.extend(desc_notes)
    if pos_name:
        rationale_parts.append(f"阳性相似 {pos_name}={pos_sim:.3f}")
    if s_evidence > 0:
        rationale_parts.append(f"证据分={s_evidence:.3f}")
    if not hits and not pos_name and s_evidence == 0:
        rationale_parts.append("弱信号：依赖描述符")

    return s_lipid, parts, attrs, "；".join(rationale_parts)
