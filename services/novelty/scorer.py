"""冻结相关参照集上的 Morgan/Tanimoto 距离；不等同官方或专利新颖性。"""

from __future__ import annotations

from dataclasses import dataclass

from packages.chem_core import clamp
from packages.goldset import GoldSet, max_similarity
from services.pipeline.config_loader import AppConfig


@dataclass(frozen=True)
class NoveltyAssessment:
    score: float
    max_similarity: float
    nearest_reference: str
    reference_version: str
    method: str = "morgan_tanimoto_distance"


def assess_structural_novelty(fp_bits, gold: GoldSet, cfg: AppConfig) -> NoveltyAssessment:
    """计算 ``1-max_similarity``；数据库 presence 和查询 miss 均不参与。"""
    policy = cfg.novelty
    reference_version = str(policy.get("reference_version") or "unversioned")
    unknown_score = clamp(float(policy.get("unknown_score", 0.5)))
    if fp_bits is None or not gold.positives:
        return NoveltyAssessment(
            score=unknown_score,
            max_similarity=0.0,
            nearest_reference="",
            reference_version=reference_version,
        )
    similarity, name = max_similarity(fp_bits, gold.positives)
    return NoveltyAssessment(
        score=clamp(1.0 - similarity),
        max_similarity=clamp(similarity),
        nearest_reference=name or "",
        reference_version=reference_version,
    )
