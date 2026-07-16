"""有效命中代理：降脂达标 AND 毒性风险可接受。"""

from __future__ import annotations

from packages.models import (
    EligibilityDecision,
    EligibilityPolicy,
    MoleculeAssessment,
)


def policy_from_config(gates: dict[str, float]) -> EligibilityPolicy:
    return EligibilityPolicy(
        lipid_min=float(gates["lipid_min"]),
        tox_hard=float(gates["tox_hard"]),
        tox_nomination_max=float(gates.get("tox_nomination_max", gates.get("tox_soft", 0.45))),
        min_toxicity_confidence=float(gates.get("min_toxicity_confidence", 0.0)),
        min_safety_evidence_coverage=float(
            gates.get(
                "min_safety_evidence_coverage",
                gates.get("min_toxicity_confidence", 0.0),
            )
        ),
    )


def evaluate_candidate_eligibility(
    molecule_result: MoleculeAssessment,
    policy: EligibilityPolicy,
) -> EligibilityDecision:
    """给出唯一、可复用的资格决定；高降脂分不能抵消不可接受毒性。"""
    lipid_pass = molecule_result.lipid_score >= policy.lipid_min
    hard_toxicity_pass = molecule_result.toxicity_score < policy.tox_hard
    nomination_risk = (
        molecule_result.toxicity_upper_bound
        if molecule_result.toxicity_upper_bound is not None
        else molecule_result.toxicity_score
    )
    toxicity_pass = nomination_risk < policy.tox_nomination_max
    safety_confidence = (
        molecule_result.safety_clearance_confidence
        if molecule_result.safety_clearance_confidence is not None
        else molecule_result.toxicity_confidence
    )
    evidence_coverage = (
        molecule_result.toxicity_evidence_coverage
        if molecule_result.toxicity_evidence_coverage is not None
        else molecule_result.toxicity_confidence
    )
    # coverage=风险/任务证据覆盖；clearance=外部安全清除可信度。二者独立门控，
    # 配置名 min_safety_evidence_coverage 历史遗留，实际比较 toxicity_evidence_coverage。
    confidence_pass = safety_confidence >= policy.min_toxicity_confidence
    evidence_coverage_pass = evidence_coverage >= policy.min_safety_evidence_coverage
    reasons: list[str] = []
    if not lipid_pass:
        reasons.append(
            f"S_lipid {molecule_result.lipid_score:.3f} < lipid_min {policy.lipid_min:.3f}"
        )
    if not hard_toxicity_pass:
        reasons.append(
            f"R_tox {molecule_result.toxicity_score:.3f} >= tox_hard {policy.tox_hard:.3f}"
        )
    elif not toxicity_pass:
        reasons.append(
            f"conservative_R_tox {nomination_risk:.3f} >= automatic_nomination_max "
            f"{policy.tox_nomination_max:.3f}; manual_review_required"
        )
    if not confidence_pass:
        reasons.append(
            "safety_clearance_confidence "
            f"{safety_confidence:.3f} < minimum "
            f"{policy.min_toxicity_confidence:.3f}"
        )
    if not evidence_coverage_pass:
        reasons.append(
            "toxicity_evidence_coverage "
            f"{evidence_coverage:.3f} < minimum "
            f"{policy.min_safety_evidence_coverage:.3f}"
        )

    if not lipid_pass or not hard_toxicity_pass:
        status = "ineligible"
    elif not toxicity_pass or not confidence_pass or not evidence_coverage_pass:
        status = "review_required"
    else:
        status = "eligible"
        reasons.append("lipid_and_toxicity_policy_passed")
    return EligibilityDecision(
        status=status,
        reasons=tuple(reasons),
        lipid_pass=lipid_pass,
        toxicity_pass=toxicity_pass,
        hard_toxicity_pass=hard_toxicity_pass,
        confidence_pass=confidence_pass,
        evidence_coverage_pass=evidence_coverage_pass,
    )
