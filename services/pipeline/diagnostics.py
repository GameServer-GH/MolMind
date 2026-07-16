"""阶段 3 诊断：RunDiagnostics + quality_gates 告警位（不全库 raise）。"""

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
    parse_success_rate = input_count / raw_count if raw_count else 0.0
    missing_inchikey_rate = inchikey_missing / input_count if input_count else 1.0
    mean_tox_coverage = (
        statistics.fmean(m.toxicity_evidence_coverage for m in eligible)
        if eligible
        else 0.0
    )

    qg = cfg.quality_gates
    notes: list[str] = []
    ml_note = cfg.finalize_ml_run_stats()
    if ml_note:
        notes.append(ml_note)

    fp_ok = _yluo_fp_gate_pass(fp_in_top, int(qg.get("max_goldset_fp_in_top10", 0)))
    if not fp_ok:
        notes.append(f"quality: goldset_fp_in_top10={fp_in_top} exceeds gate")

    if float(qg.get("min_std_tox", 0.0) or 0.0) > 0:
        notes.append("legacy min_std_tox ignored: risk dispersion is not a scientific accuracy metric")

    min_scaf = int(qg.get("min_scaffold_diversity_top10", 0) or 0)
    scaf_ok = True if min_scaf <= 0 else len(scaffolds) >= min_scaf
    if not scaf_ok:
        notes.append(
            f"quality: scaffold_diversity_top10={len(scaffolds)} < min={min_scaf}"
        )

    min_parse_success = float(qg.get("min_parse_success_rate", 0.0))
    max_missing_key = float(qg.get("max_missing_inchikey_rate", 1.0))
    ids = [m.molecule_id for m in top]
    engineering_pass = (
        parse_success_rate >= min_parse_success
        and missing_inchikey_rate <= max_missing_key
        and len(ids) == len(set(ids))
        and all(m.eligibility_status == "eligible" and not m.gated_out for m in top)
    )
    if parse_success_rate < min_parse_success:
        notes.append(
            f"engineering: parse_success_rate={parse_success_rate:.4f} < {min_parse_success:.4f}"
        )
    if missing_inchikey_rate > max_missing_key:
        notes.append(
            f"engineering: missing_inchikey_rate={missing_inchikey_rate:.4f} > {max_missing_key:.4f}"
        )
    if len(ids) != len(set(ids)):
        notes.append("engineering: duplicate molecule_id in final candidates")

    min_coverage = float(qg.get("min_model_coverage_warning", 0.0))
    # 避免 0.20 的二进制浮点均值被误报为 “0.2000 < 0.2000”。
    model_coverage_status = (
        "adequate" if mean_tox_coverage + 1e-12 >= min_coverage else "warning"
    )
    if model_coverage_status == "warning":
        notes.append(
            f"model_coverage: mean_toxicity_coverage={mean_tox_coverage:.4f} < {min_coverage:.4f}"
        )
    if not fp_ok:
        notes.append("scientific_warning: GoldSet FP is regression evidence, not independent validation")
    if not scaf_ok:
        notes.append("portfolio_warning: scaffold diversity below configured target")
    if evidence_hit_count == 0:
        notes.append(
            "scientific_warning: no candidate-specific lipid/toxicity evidence hit; "
            "ranking is proxy-only"
        )

    # 只有工程不变量可给 PASS/FAIL；无独立双终点数据时科学性能明确为 unavailable。
    quality_pass = engineering_pass

    return RunDiagnostics(
        input_count=input_count,
        filtered_out=filtered_out,
        eligible_count=len(eligible),
        ro5_pass_rate=round(ro5_pass, 4),
        std_tox=round(std_tox, 4),
        scaffold_diversity_top10=len(scaffolds),
        evidence_coverage_ratio=round(evidence_cov, 4),
        goldset_fp_in_top10=fp_in_top,
        goldset_pos_percentile=None,  # 全库百分位需对照集镶嵌，暂不计算
        config_hash=cfg.config_hash,
        mode=cfg.mode,
        degraded_channels=list(cfg.degraded_channels),
        quality_pass=quality_pass,
        notes=notes,
        raw_count=raw_count,
        parse_skipped=parse_skipped,
        inchikey_missing=inchikey_missing,
        engineering_pass=engineering_pass,
        model_coverage_status=model_coverage_status,
        scientific_validation_status="not_available_no_independent_dual_endpoint_benchmark",
    )
