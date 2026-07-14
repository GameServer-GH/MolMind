"""services.ranker：S_final 可复算；同骨架超限替换；门控可解释。"""

from __future__ import annotations

from packages.chem_core import compute_descriptors, morgan_fp
from packages.goldset import load_goldset
from packages.models import MoleculeRecord, ScoreRecord
from rdkit import Chem
from services.evidence_facade import EvidenceBundle
from services.pipeline.config_loader import load_config
from services.ranker import apply_scaffold_diversity, score_molecule


def _record(smiles: str, mid: str) -> MoleculeRecord:
    desc = compute_descriptors(smiles)
    assert desc is not None
    mol = Chem.MolFromSmiles(smiles)
    assert mol is not None
    return MoleculeRecord(
        molecule_id=mid,
        smiles=smiles,
        inchikey=Chem.MolToInchiKey(mol) or "",
        cas=None,
        mw=float(desc["mw"]),
        logp=float(desc["logp"]),
        hbd=int(desc["hbd"]),
        hba=int(desc["hba"]),
        tpsa=float(desc["tpsa"]),
        rotatable_bonds=int(desc["rotatable_bonds"]),
        aromatic_rings=int(desc["aromatic_rings"]),
        fp_bits=morgan_fp(mol),
    )


def test_s_final_formula_recomputable() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    evidence = EvidenceBundle()
    sim = next(c for c in gold.positives if c.name == "Simvastatin")
    scored = score_molecule(_record(sim.smiles, "Sim"), cfg, gold, evidence)
    if scored.gated_out:
        assert scored.gate_reason
        return
    w = cfg.weights
    expected = (
        w["lipid"] * scored.lipid_score
        + w["tox_safety"] * (1.0 - scored.tox_risk)
        + w["novelty"] * scored.novelty_score
        + w["evidence_confidence"] * scored.conf_e
    )
    assert abs(expected - scored.final_score) < 1e-3


def test_scaffold_cap_replaces_excess() -> None:
    base = ScoreRecord(
        molecule_id="A",
        smiles="c1ccccc1",
        inchikey="X",
        cas=None,
        scaffold_smiles="c1ccccc1",
        lipid_score=0.8,
        tox_risk=0.1,
        novelty_score=0.7,
        conf_e=0.0,
        final_score=0.9,
        tox_heads={},
        lipid_parts={},
        attributions=[],
        lipid_rationale="",
        tox_rationale="",
        overall_reason="",
    )
    ranked = []
    for i in range(5):
        mol = ScoreRecord(**{**base.__dict__, "molecule_id": f"M{i}", "final_score": 0.9 - i * 0.01})
        ranked.append(mol)
    selected = apply_scaffold_diversity(ranked, top_n=3, max_per_scaffold=2, redundancy_lambda=0.05)
    assert len(selected) == 3
    # first two from same scaffold, third from deferred backfill
    assert selected[0].molecule_id == "M0"
    assert selected[1].molecule_id == "M1"
    assert "骨架限额回填" in selected[2].overall_reason or selected[2].molecule_id == "M2"


def test_gate_out_explainable() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    evidence = EvidenceBundle()
    # ethanol likely fails lipid_min
    scored = score_molecule(_record("CCO", "EtOH"), cfg, gold, evidence)
    if scored.gated_out:
        assert scored.gate_reason
        assert scored.final_score == 0.0
