"""确定性权重/门槛扰动诊断；只审计，不静默改变主排名。"""

from __future__ import annotations

import statistics

from packages.models import MoleculeAssessment, ScoreRecord
from services.eligibility import evaluate_candidate_eligibility, policy_from_config
from services.pipeline.config_loader import AppConfig


def _renormalized_weight_variant(
    weights: dict[str, float], target: str, delta: float
) -> dict[str, float]:
    varied = dict(weights)
    new_target = min(1.0, max(0.0, varied[target] + delta))
    remaining_old = 1.0 - varied[target]
    remaining_new = 1.0 - new_target
    varied[target] = new_target
    if remaining_old <= 0:
        share = remaining_new / max(1, len(varied) - 1)
        for key in varied:
            if key != target:
                varied[key] = share
    else:
        for key in varied:
            if key != target:
                varied[key] = varied[key] * remaining_new / remaining_old
    return varied


def _weighted_score(mol: ScoreRecord, weights: dict[str, float]) -> float:
    return (
        weights["lipid"] * mol.lipid_score
        + weights["tox_safety"] * (1.0 - mol.tox_risk)
        + weights["novelty"] * mol.novelty_score
        + weights["evidence_confidence"] * mol.conf_e
    )


def analyze_rank_robustness(
    candidates: list[ScoreRecord],
    cfg: AppConfig,
    *,
    top_n: int,
) -> list[dict[str, object]]:
    """返回候选级稳定性表；base + 单权重正负扰动 + 门槛正负扰动。"""
    if not candidates:
        return []
    policy_cfg = cfg.robustness
    if not bool(policy_cfg.get("enabled", True)):
        return []
    weight_delta = float(policy_cfg.get("weight_delta", 0.05))
    gate_delta = float(policy_cfg.get("gate_delta", 0.03))
    base_weights = cfg.weights
    scenarios: list[tuple[str, dict[str, float], dict[str, float]]] = [
        ("base", base_weights, cfg.gates)
    ]
    for target in sorted(base_weights):
        for sign in (-1.0, 1.0):
            scenarios.append(
                (
                    f"weight:{target}:{sign:+.0f}",
                    _renormalized_weight_variant(base_weights, target, sign * weight_delta),
                    cfg.gates,
                )
            )
    for gate_name in ("lipid_min", "tox_nomination_max"):
        for sign in (-1.0, 1.0):
            gates = cfg.gates
            gates[gate_name] = min(1.0, max(0.0, gates[gate_name] + sign * gate_delta))
            scenarios.append((f"gate:{gate_name}:{sign:+.0f}", base_weights, gates))

    ranks: dict[str, list[int]] = {mol.molecule_id: [] for mol in candidates}
    scores: dict[str, list[float]] = {mol.molecule_id: [] for mol in candidates}
    included: dict[str, int] = {mol.molecule_id: 0 for mol in candidates}
    for _name, weights, gates in scenarios:
        policy = policy_from_config(gates)
        eligible: list[tuple[float, str, ScoreRecord]] = []
        for mol in candidates:
            decision = evaluate_candidate_eligibility(
                MoleculeAssessment(
                    molecule_id=mol.molecule_id,
                    lipid_score=mol.lipid_score,
                    toxicity_score=mol.tox_risk,
                    toxicity_confidence=mol.toxicity_confidence,
                    toxicity_evidence_coverage=mol.toxicity_evidence_coverage,
                    safety_clearance_confidence=mol.safety_clearance_confidence,
                    toxicity_upper_bound=mol.tox_upper_bound,
                ),
                policy,
            )
            if not decision.is_eligible:
                continue
            score = _weighted_score(mol, weights)
            eligible.append((score, mol.molecule_id, mol))
            scores[mol.molecule_id].append(score)
        eligible.sort(key=lambda item: (-item[0], item[1]))
        for rank, (_score, _mid, mol) in enumerate(eligible, start=1):
            ranks[mol.molecule_id].append(rank)
            if rank <= top_n:
                included[mol.molecule_id] += 1

    total = len(scenarios)
    rows: list[dict[str, object]] = []
    for mol in sorted(candidates, key=lambda item: (-item.final_score, item.molecule_id)):
        mol_ranks = ranks[mol.molecule_id]
        frequency = included[mol.molecule_id] / total
        mol.robust_inclusion_frequency = round(frequency, 4)
        mol.robust_rank_median = round(float(statistics.median(mol_ranks)), 2) if mol_ranks else None
        mol.robust_rank_min = min(mol_ranks) if mol_ranks else None
        mol.robust_rank_max = max(mol_ranks) if mol_ranks else None
        rows.append(
            {
                "molecule_id": mol.molecule_id,
                "scenario_count": total,
                "top_n": top_n,
                "inclusion_frequency": mol.robust_inclusion_frequency,
                "rank_median": mol.robust_rank_median,
                "rank_min": mol.robust_rank_min,
                "rank_max": mol.robust_rank_max,
                "score_min": round(min(scores[mol.molecule_id]), 4) if scores[mol.molecule_id] else None,
                "score_max": round(max(scores[mol.molecule_id]), 4) if scores[mol.molecule_id] else None,
            }
        )
    return rows
