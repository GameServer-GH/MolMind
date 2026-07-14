"""阶段 3 诊断桩：计算基础 RunDiagnostics（阶段 5 扩展）。"""

from __future__ import annotations

import statistics

from packages.goldset import GoldSet, max_similarity
from packages.models import RunDiagnostics, ScoreRecord
from services.pipeline.config_loader import AppConfig


def _yluo_fp_gate_pass(fp_in_top: int, max_allowed: int) -> bool:
    """GoldSet false-positive gate (YLuo quality helper)."""
    return fp_in_top <= max_allowed


def compute_diagnostics(
    *,
    cfg: AppConfig,
    gold: GoldSet,
    input_count: int,
    filtered_out: int,
    scored: list[ScoreRecord],
    eligible: list[ScoreRecord],
    top: list[ScoreRecord],
    evidence_hit_count: int,
    raw_count: int = 0,
    parse_skipped: int = 0,
    inchikey_missing: int = 0,
) -> RunDiagnostics:
    tox_vals = [m.tox_risk for m in eligible] or [m.tox_risk for m in scored]
    std_tox = float(statistics.pstdev(tox_vals)) if len(tox_vals) >= 2 else 0.0
    scaffolds = {m.scaffold_smiles for m in top if m.scaffold_smiles}
    fp_in_top = 0
    for mol in top:
        sim, _ = max_similarity(mol.fp_bits, gold.false_positives)
        if sim >= float(cfg.critic.get("fp_sim_threshold", 0.75)):
            fp_in_top += 1
    ro5_pass = (input_count - filtered_out) / input_count if input_count else 0.0
    evidence_cov = evidence_hit_count / len(eligible) if eligible else 0.0
    quality_pass = _yluo_fp_gate_pass(
        fp_in_top, int(cfg.quality_gates.get("max_goldset_fp_in_top10", 0))
    )
    return RunDiagnostics(
        input_count=input_count,
        filtered_out=filtered_out,
        eligible_count=len(eligible),
        ro5_pass_rate=round(ro5_pass, 4),
        std_tox=round(std_tox, 4),
        scaffold_diversity_top10=len(scaffolds),
        evidence_coverage_ratio=round(evidence_cov, 4),
        goldset_fp_in_top10=fp_in_top,
        goldset_pos_percentile=None,
        config_hash=cfg.config_hash,
        mode=cfg.mode,
        degraded_channels=list(cfg.degraded_channels),
        quality_pass=quality_pass,
        raw_count=raw_count,
        parse_skipped=parse_skipped,
        inchikey_missing=inchikey_missing,
    )
