"""综合排序 + Murcko scaffold 多样性限额。"""

from __future__ import annotations

from collections import Counter

from packages.chem_core import clamp, murcko_scaffold_smiles
from packages.goldset import GoldSet, max_similarity
from packages.models import Attribution, MoleculeRecord, ScoreRecord
from services.evidence_facade.bundle import EvidenceBundle
from services.pipeline.config_loader import AppConfig
from services.scorer_lipid import score_lipid
from services.scorer_tox import score_tox


def _ljr_novelty_with_known_penalty(
    evidence: EvidenceBundle,
    gold: GoldSet,
    fp_bits,
    cfg: AppConfig,
) -> tuple[float, str | None]:
    """Novelty prior with known-positive dampening (LJR)."""
    novelty = clamp(evidence.novelty_score)
    pos_sim, pos_name = max_similarity(fp_bits, gold.positives)
    exact = float(cfg.critic.get("known_positive_sim", 0.98))
    near = float(cfg.critic.get("near_positive_sim", 0.85))
    note = None
    if pos_sim >= exact:
        novelty = min(novelty, 0.05)
        note = f"库内≈阳性对照 {pos_name}（sim={pos_sim:.3f}），新颖性压低"
    elif pos_sim >= near:
        novelty = min(novelty, 0.25)
        note = f"高度近似阳性 {pos_name}（sim={pos_sim:.3f}），新颖性下调"
    return novelty, note


def score_molecule(
    record: MoleculeRecord,
    cfg: AppConfig,
    gold: GoldSet,
    evidence: EvidenceBundle,
) -> ScoreRecord:
    lipid_score, lipid_parts, lipid_attrs, lipid_rationale = score_lipid(
        record, cfg, gold, evidence
    )
    tox_risk, tox_heads, _boost, tox_attrs, tox_rationale = score_tox(
        record, cfg, gold, evidence
    )
    novelty, novelty_note = _ljr_novelty_with_known_penalty(evidence, gold, record.fp_bits, cfg)
    conf_e = clamp(evidence.conf_e)

    gates = cfg.gates
    gated_out = False
    gate_reason = ""
    if tox_risk > float(gates["tox_hard"]):
        gated_out = True
        gate_reason = f"R_tox {tox_risk:.3f} > τ_hard {gates['tox_hard']}"
    elif lipid_score < float(gates["lipid_min"]):
        gated_out = True
        gate_reason = f"S_lipid {lipid_score:.3f} < τ_min {gates['lipid_min']}"

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

    scaffold = murcko_scaffold_smiles(record.smiles)
    attrs = lipid_attrs + tox_attrs
    attrs.append(Attribution("scaffold", scaffold or "none"))
    if novelty_note:
        attrs.append(Attribution("novelty", novelty_note, value=novelty))

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
        gated_out=gated_out,
        gate_reason=gate_reason,
        fp_bits=record.fp_bits,
    )


def apply_scaffold_diversity(
    ranked: list[ScoreRecord],
    *,
    top_n: int,
    max_per_scaffold: int,
    redundancy_lambda: float,
) -> list[ScoreRecord]:
    selected: list[ScoreRecord] = []
    scaffold_counts: Counter[str] = Counter()
    deferred: list[ScoreRecord] = []

    for mol in ranked:
        key = mol.scaffold_smiles or mol.molecule_id
        if scaffold_counts[key] >= max_per_scaffold:
            deferred.append(mol)
            continue
        if scaffold_counts[key] > 0:
            mol.overall_reason += f"；scaffold_slot={scaffold_counts[key] + 1}/{max_per_scaffold}"
            if redundancy_lambda > 0:
                mol.overall_reason += f"；λ_redundancy={redundancy_lambda}"
        selected.append(mol)
        scaffold_counts[key] += 1
        if len(selected) >= top_n:
            break

    if len(selected) < top_n:
        for mol in deferred:
            if len(selected) >= top_n:
                break
            mol.overall_reason += "；骨架限额回填"
            selected.append(mol)

    return selected
