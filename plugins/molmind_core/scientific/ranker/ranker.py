"""综合排序 + Murcko scaffold 多样性限额。"""

from __future__ import annotations

from collections import Counter

from packages.chem_core import clamp, murcko_scaffold_smiles, tanimoto
from packages.goldset import GoldSet, max_similarity
from packages.models import (
    Attribution,
    EligibilityDecision,
    MoleculeAssessment,
    MoleculeRecord,
    ScoreRecord,
    format_selection_reason,
)
from plugins.molmind_core.scientific.eligibility import evaluate_candidate_eligibility, policy_from_config
from plugins.molmind_core.scientific.evidence_facade.bundle import EvidenceBundle
from plugins.molmind_core.scientific.evidence_facade.citations import citations_from_hits
from plugins.molmind_core.scientific.novelty import assess_structural_novelty
from plugins.molmind_core.scientific.pipeline.config_loader import AppConfig
from plugins.molmind_core.scientific.scorer_lipid import score_lipid
from plugins.molmind_core.scientific.scorer_tox import score_tox


COMPETITION_SCORING_VERSION = "organizer-relative-effect-novelty-v1"


def competition_selection_score(mol: ScoreRecord) -> float:
    """Return the frozen portfolio score, falling back for legacy/unit records."""
    if mol.competition_scoring_version != "unassigned":
        return float(mol.selection_score)
    return float(mol.final_score)


def assign_competition_scores(
    candidates: list[ScoreRecord], cfg: AppConfig
) -> list[ScoreRecord]:
    """Assign deterministic run-relative effect × novelty scoring proxies.

    Ranking emphasizes relative effect × novelty (product primary, equal-mean
    sensitivity). The absolute normalization formula is not claimed; we freeze a
    transparent ordinal percentile proxy and export both views.
    """
    if not candidates:
        return candidates
    policy = cfg.competition_scoring
    enabled = bool(policy.get("enabled", True))
    primary = str(policy.get("primary") or "product")
    n = len(candidates)

    effect_order = sorted(candidates, key=lambda m: (-m.lipid_score, m.molecule_id))
    novelty_order = sorted(candidates, key=lambda m: (-m.novelty_score, m.molecule_id))
    effect_ranks = {m.molecule_id: rank for rank, m in enumerate(effect_order, start=1)}
    novelty_ranks = {m.molecule_id: rank for rank, m in enumerate(novelty_order, start=1)}

    def percentile(rank: int) -> float:
        return 1.0 if n == 1 else (n - rank) / (n - 1)

    for mol in candidates:
        effect_rank = effect_ranks[mol.molecule_id]
        novelty_rank = novelty_ranks[mol.molecule_id]
        effect = percentile(effect_rank)
        novelty = percentile(novelty_rank)
        product = effect * novelty
        equal_mean = 0.5 * (effect + novelty)
        selection = product if primary == "product" else equal_mean
        if not enabled:
            selection = mol.final_score
        mol.effect_proxy_score = round(effect, 6)
        mol.novelty_proxy_score = round(novelty, 6)
        mol.effect_rank = effect_rank
        mol.novelty_rank = novelty_rank
        mol.effect_x_novelty = round(product, 6)
        mol.effect_novelty_equal_mean = round(equal_mean, 6)
        mol.selection_score = round(selection, 6)
        mol.competition_scoring_version = COMPETITION_SCORING_VERSION
        mol.selection_factors = dict(mol.selection_factors or {})
        mol.selection_factors["score"] = (
            f"competition={mol.selection_score:.4f};legacy={mol.final_score:.4f};"
            f"effect_rank={effect_rank};novelty_rank={novelty_rank}"
        )
        mol.selection_reason = format_selection_reason(mol.selection_factors)
    candidates.sort(key=lambda m: (-competition_selection_score(m), m.molecule_id))
    return candidates


def _scientific_claim(evidence: EvidenceBundle) -> tuple[str, str, tuple[str, ...]]:
    """Separate computational eligibility from the maximum scientific claim."""
    lipid_task = [
        hit for hit in evidence.lipid
        if hit.evidence_role == "task_evidence"
        and hit.direction not in {"contradicts", "negative", "risk"}
        and hit.evidence_type != "identity_annotation"
    ]
    has_l4 = any(
        str(hit.payload.get("evidence_level") or "").upper() == "L4"
        and bool(hit.payload.get("paired_endpoints"))
        for hit in lipid_task
    )
    has_identity_review = evidence.has_identity_review_required
    missing: list[str] = []
    if not lipid_task:
        missing.append("lipid_activity")
    if not evidence.has_safety_clearance_evidence:
        missing.append("safety_clearance")
    if has_identity_review:
        return "identity_review_required", "audit_only", tuple(missing)
    if has_l4:
        return "task_specific_experiment_supported", "same_condition_dual_endpoint", tuple(missing)
    if lipid_task:
        return "candidate_activity_evidence", "candidate_level_activity", tuple(missing)
    if evidence.pathway or any(
        hit.evidence_role == "mechanism_support" for hit in evidence.all_hits()
    ):
        return "mechanism_support_only", "mechanism_hypothesis", tuple(missing)
    if evidence.tox:
        return "risk_evidence_only", "risk_signal_only", tuple(missing)
    # annotation_only / database presence 不得抬升到候选活性证据。
    return "proxy_only", "proxy_nomination", tuple(missing or ("lipid_activity", "safety_clearance"))


def score_molecule(
    record: MoleculeRecord,
    cfg: AppConfig,
    gold: GoldSet,
    evidence: EvidenceBundle,
    *,
    excluded_reference_names: set[str] | None = None,
) -> ScoreRecord:
    # 同一分子对 goldset 子集的 max Tanimoto 只算一次，供 lipid/novelty/tox 复用。
    if record.fp_bits is None:
        positive_similarity: tuple[float, str | None] = (0.0, None)
        false_positive_similarity: tuple[float, str | None] = (0.0, None)
        negative_similarity: tuple[float, str | None] = (0.0, None)
    else:
        positive_similarity = max_similarity(record.fp_bits, gold.positives)
        false_positive_similarity = max_similarity(record.fp_bits, gold.false_positives)
        negative_similarity = max_similarity(record.fp_bits, gold.negatives)

    lipid_score, lipid_parts, lipid_attrs, lipid_rationale = score_lipid(
        record,
        cfg,
        gold,
        evidence,
        positive_similarity=positive_similarity,
    )
    tox_risk, tox_heads, _boost, tox_attrs, tox_rationale = score_tox(
        record,
        cfg,
        gold,
        evidence,
        excluded_reference_names=excluded_reference_names,
        false_positive_similarity=false_positive_similarity,
        negative_similarity=negative_similarity,
    )
    scientific_status, claim_ceiling, audit_missing = _scientific_claim(evidence)
    novelty_assessment = assess_structural_novelty(
        record.fp_bits,
        gold,
        cfg,
        positive_similarity=positive_similarity,
    )
    novelty = novelty_assessment.score
    novelty_note = (
        f"透明新颖性代理: 1-max_tanimoto={novelty:.3f}; "
        f"nearest={novelty_assessment.nearest_reference or 'none'} "
        f"sim={novelty_assessment.max_similarity:.3f}; "
        f"reference={novelty_assessment.reference_version}"
    )
    conf_e = clamp(evidence.lipid_evidence_confidence)

    gates = cfg.gates
    tox_confidence = float(tox_heads.get("confidence", 0.0))
    tox_uncertainty = float(tox_heads.get("uncertainty", 1.0))
    tox_coverage = float(tox_heads.get("evidence_coverage", 0.0))
    safety_clearance = float(tox_heads.get("safety_clearance_confidence", tox_confidence))
    proxy_clearance = float(tox_heads.get("proxy_clearance_confidence", 0.0))
    tox_upper_bound = float(tox_heads.get("tox_upper_bound", tox_risk))
    eligibility = evaluate_candidate_eligibility(
        MoleculeAssessment(
            molecule_id=record.molecule_id,
            lipid_score=lipid_score,
            toxicity_score=tox_risk,
            toxicity_confidence=tox_confidence,
            toxicity_evidence_coverage=tox_coverage,
            safety_clearance_confidence=safety_clearance,
            toxicity_upper_bound=tox_upper_bound,
        ),
        policy_from_config(gates),
    )
    # 身份歧义：禁止静默入榜。仅硬毒性失败保留 ineligible；
    # 其余（含降脂略低）统一进入 review_required，等待身份确认。
    if evidence.has_identity_review_required and scientific_status == "identity_review_required":
        reasons = eligibility.reasons
        if "identity_review_required" not in reasons:
            reasons = reasons + ("identity_review_required",)
        status = (
            "ineligible"
            if not eligibility.hard_toxicity_pass
            else "review_required"
        )
        eligibility = EligibilityDecision(
            status=status,
            reasons=reasons,
            lipid_pass=eligibility.lipid_pass,
            toxicity_pass=eligibility.toxicity_pass,
            hard_toxicity_pass=eligibility.hard_toxicity_pass,
            confidence_pass=eligibility.confidence_pass,
            evidence_coverage_pass=eligibility.evidence_coverage_pass,
        )
    dili_audit = dict(evidence.dili_audit or {})
    if str(dili_audit.get("action") or "") == "hard_exclude":
        reasons = tuple(eligibility.reasons or ())
        if "dilirank_most_exact" not in reasons:
            reasons = reasons + ("dilirank_most_exact",)
        eligibility = EligibilityDecision(
            status="ineligible",
            reasons=reasons,
            lipid_pass=eligibility.lipid_pass,
            toxicity_pass=False,
            hard_toxicity_pass=False,
            confidence_pass=eligibility.confidence_pass,
            evidence_coverage_pass=eligibility.evidence_coverage_pass,
        )
        dili_note = (
            f"dilirank_exact_gate:{dili_audit.get('compound_name') or dili_audit.get('ltkb_id')} "
            f"concern={dili_audit.get('concern')} basis={dili_audit.get('match_basis')}"
        )
        tox_rationale = f"{tox_rationale}; {dili_note}" if tox_rationale else dili_note
    gated_out = not eligibility.is_eligible
    gate_reason = "; ".join(eligibility.reasons) if gated_out else ""

    w = cfg.weights
    final = 0.0
    overall = gate_reason
    if not gated_out:
        final = (
            w["lipid"] * lipid_score
            + w["tox_safety"] * (1.0 - tox_risk)
            + w["novelty"] * novelty
            + w["evidence_confidence"] * conf_e
        )
        overall = (
            f"S_final={w['lipid']:.2f}×S_lipid({lipid_score:.3f})"
            f"+{w['tox_safety']:.2f}×(1-R_tox)({1 - tox_risk:.3f})"
            f"+{w['novelty']:.2f}×novel({novelty:.3f})"
            f"+{w['evidence_confidence']:.2f}×conf_e({conf_e:.3f})"
            f"={final:.4f}"
        )
        if novelty_note:
            overall += f"；{novelty_note}"
        # P1-C：软毒性审慎标记（不改分、不门控）
        tox_soft = float(gates.get("tox_soft", 0.45))
        tox_hard = float(gates.get("tox_hard", 0.65))
        if tox_soft < tox_risk <= tox_hard and lipid_score >= float(gates.get("lipid_min", 0.35)):
            overall += (
                f"；soft_tox_caution: S_lipid={lipid_score:.3f} 且 "
                f"tox_soft({tox_soft})<R_tox({tox_risk:.3f})≤tox_hard({tox_hard})"
            )

    scaffold = murcko_scaffold_smiles(record.smiles)
    attrs = lipid_attrs + tox_attrs
    attrs.append(Attribution("scaffold", scaffold or "none"))
    if novelty_note:
        attrs.append(Attribution("novelty", novelty_note, value=novelty))

    selection_factors = {
        "eligibility": eligibility.status,
        "score": f"{final:.4f}",
        "scaffold_diversity": "",
        "evidence_coverage": (
            f"lipid_conf={conf_e:.3f};tox_coverage={tox_coverage:.3f};"
            f"safety_clearance={safety_clearance:.3f};proxy_clearance={proxy_clearance:.3f}"
        ),
        "combo_adjustment": "",
    }
    if dili_audit.get("status") and dili_audit.get("status") != "disabled":
        selection_factors["dilirank_exact"] = (
            f"{dili_audit.get('status')}:{dili_audit.get('action')}"
        )

    return ScoreRecord(
        molecule_id=record.molecule_id,
        smiles=record.smiles,
        inchikey=record.inchikey,
        cas=record.cas,
        scaffold_smiles=scaffold,
        lipid_score=round(lipid_score, 4),
        tox_risk=round(tox_risk, 4),
        novelty_score=round(novelty, 4),
        conf_e=round(conf_e, 4),
        final_score=round(final, 4),
        tox_heads={k: round(v, 4) for k, v in tox_heads.items()},
        lipid_parts={k: round(v, 4) for k, v in lipid_parts.items()},
        attributions=attrs,
        lipid_rationale=lipid_rationale,
        tox_rationale=tox_rationale,
        overall_reason=overall,
        toxicity_confidence=round(tox_confidence, 4),
        toxicity_uncertainty=round(tox_uncertainty, 4),
        eligibility_status=eligibility.status,
        eligibility_reasons=eligibility.reasons,
        gated_out=gated_out,
        gate_reason=gate_reason,
        fp_bits=record.fp_bits,
        lipid_evidence_confidence=round(conf_e, 4),
        toxicity_model_applicability=round(float(tox_heads.get("model_applicability", 0.0)), 4),
        toxicity_evidence_coverage=round(tox_coverage, 4),
        risk_signal_confidence=round(float(tox_heads.get("risk_signal_confidence", 0.0)), 4),
        safety_clearance_confidence=round(safety_clearance, 4),
        proxy_clearance_confidence=round(proxy_clearance, 4),
        tox_upper_bound=round(tox_upper_bound, 4),
        novelty_reference_version=novelty_assessment.reference_version,
        novelty_nearest_reference=novelty_assessment.nearest_reference,
        novelty_max_similarity=round(novelty_assessment.max_similarity, 4),
        selection_factors=selection_factors,
        selection_reason=format_selection_reason(selection_factors),
        scientific_status=scientific_status,
        claim_ceiling=claim_ceiling,
        audit_missing=audit_missing,
        lipid_evidence_status=evidence.lipid_query_status,  # type: ignore[arg-type]
        toxicity_evidence_status=evidence.toxicity_query_status,  # type: ignore[arg-type]
        evidence_hits=evidence.all_hits(),
        citations=citations_from_hits(evidence.all_hits()),
        evidence_run_id=evidence.run_id,
        input_structure_hash=evidence.input_structure_hash,
        epa_audit=dict(evidence.epa_audit),
        dili_audit=dict(evidence.dili_audit or {}),
        evidence_source_audit=dict(evidence.evidence_source_audit or {}),
        screening_concentration_um=10.0,
        viability_endpoint="CCK-8",
        viability_threshold_reference=">0.80_relative_to_control",
        dual_endpoint_claim="lipid_and_viability_parallel_required",
        identity_status=(
            "review_required"
            if scientific_status == "identity_review_required"
            else "resolved"
        ),
    )


def apply_scaffold_diversity(
    ranked: list[ScoreRecord],
    *,
    top_n: int,
    max_per_scaffold: int,
    redundancy_lambda: float,
    max_pairwise_similarity: float | None = None,
    similarity_cluster_threshold: float | None = None,
    max_per_similarity_cluster: int = 1,
    mmr_lambda: float = 1.0,
) -> list[ScoreRecord]:
    """确定性 MMR + scaffold/相似簇约束；旧调用未传新参数时保持兼容。"""
    selected: list[ScoreRecord] = []
    scaffold_counts: Counter[str] = Counter()
    cluster_counts: Counter[str] = Counter()
    remaining = list(ranked)
    deferred: list[ScoreRecord] = []

    while remaining and len(selected) < top_n:
        candidates: list[tuple[float, float, str, ScoreRecord, str, float, str]] = []
        next_remaining: list[ScoreRecord] = []
        for mol in remaining:
            key = mol.scaffold_smiles or mol.molecule_id
            if scaffold_counts[key] >= max_per_scaffold:
                deferred.append(mol)
                continue
            similarities = [tanimoto(mol.fp_bits, prior.fp_bits) for prior in selected]
            nearest = max(similarities, default=0.0)
            nearest_id = selected[similarities.index(nearest)].molecule_id if similarities else ""
            if max_pairwise_similarity is not None and nearest > max_pairwise_similarity:
                deferred.append(mol)
                continue
            cluster_key = ""
            if similarity_cluster_threshold is not None and nearest >= similarity_cluster_threshold:
                nearest_mol = selected[similarities.index(nearest)]
                cluster_key = nearest_mol.similarity_cluster or nearest_id or mol.molecule_id
                if cluster_counts[cluster_key] >= max_per_similarity_cluster:
                    deferred.append(mol)
                    continue
            portfolio_score = competition_selection_score(mol)
            mmr = mmr_lambda * portfolio_score + (1.0 - mmr_lambda) * (1.0 - nearest)
            if redundancy_lambda > 0:
                mmr -= redundancy_lambda * nearest
            candidates.append(
                (mmr, portfolio_score, mol.molecule_id, mol, cluster_key, nearest, nearest_id)
            )
            next_remaining.append(mol)
        if not candidates:
            break
        _mmr, _score, _mid, chosen, cluster_key, nearest, nearest_id = sorted(
            candidates, key=lambda item: (-item[0], -item[1], item[2])
        )[0]
        chosen.internal_nearest_similarity = round(nearest, 4)
        chosen.similarity_cluster = cluster_key or chosen.molecule_id
        chosen.selection_tier = "similarity_strict"
        chosen.selection_factors = dict(chosen.selection_factors or {})
        chosen.selection_factors["score"] = (
            f"competition={competition_selection_score(chosen):.4f};"
            f"legacy={chosen.final_score:.4f}"
        )
        chosen.selection_factors["scaffold_diversity"] = (
            f"MMR={_mmr:.4f}; nearest_selected={nearest_id or 'none'}; sim={nearest:.3f}"
        )
        chosen.selection_reason = format_selection_reason(chosen.selection_factors)
        chosen.overall_reason += f"；{chosen.selection_reason}"
        selected.append(chosen)
        key = chosen.scaffold_smiles or chosen.molecule_id
        scaffold_counts[key] += 1
        cluster_counts[chosen.similarity_cluster] += 1
        remaining = [mol for mol in remaining if mol.molecule_id != chosen.molecule_id]

    if len(selected) < top_n:
        seen = {mol.molecule_id for mol in selected}
        for mol in sorted(
            deferred + remaining,
            key=lambda item: (-competition_selection_score(item), item.molecule_id),
        ):
            if len(selected) >= top_n or mol.molecule_id in seen:
                continue
            similarities = [tanimoto(mol.fp_bits, prior.fp_bits) for prior in selected]
            nearest = max(similarities, default=0.0)
            mol.internal_nearest_similarity = round(nearest, 4)
            mol.selection_tier = "diversity_relaxed"
            mol.selection_factors = dict(mol.selection_factors or {})
            mol.selection_factors["score"] = (
                f"competition={competition_selection_score(mol):.4f};"
                f"legacy={mol.final_score:.4f}"
            )
            mol.selection_factors["scaffold_diversity"] = (
                f"diversity_relaxed; nearest_similarity={nearest:.3f}"
            )
            mol.selection_factors["combo_adjustment"] = "diversity_constraint_relaxed"
            mol.selection_reason = format_selection_reason(mol.selection_factors)
            mol.overall_reason += f"；{mol.selection_reason}"
            selected.append(mol)
            seen.add(mol.molecule_id)

    return selected
