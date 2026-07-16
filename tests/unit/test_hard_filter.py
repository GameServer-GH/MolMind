"""services.hard_filter：硝基拒；Simvastatin 过；通过率可统计。"""

from __future__ import annotations

from pathlib import Path

from packages.chem_core import compute_descriptors, morgan_fp
from packages.goldset import load_goldset
from packages.models import MoleculeRecord
from rdkit import Chem
from services.hard_filter import apply_hard_filters
from services.ingest import parse_sdf
from services.pipeline.config_loader import load_config

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SDF = ROOT / "data" / "sample.sdf"


def _record_from_smiles(molecule_id: str, smiles: str) -> MoleculeRecord:
    desc = compute_descriptors(smiles)
    assert desc is not None
    mol = Chem.MolFromSmiles(smiles)
    assert mol is not None
    return MoleculeRecord(
        molecule_id=molecule_id,
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


def test_nitro_requires_review_not_automatic_rejection() -> None:
    cfg = load_config(mode="offline")
    # nitrobenzene
    rec = _record_from_smiles("NITRO_TEST", "c1ccc(cc1)[N+](=O)[O-]")
    decision = apply_hard_filters(rec, cfg)
    assert decision.passed is True
    assert decision.status == "review_required"
    assert any(hit.rule_id == "nitro_aromatic" for hit in decision.alert_hits)
    assert all(hit.rule_id != "aniline" for hit in decision.alert_hits)


def test_simvastatin_passes() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    sim = next(c for c in gold.positives if c.name == "Simvastatin")
    rec = _record_from_smiles("Simvastatin", sim.smiles)
    decision = apply_hard_filters(rec, cfg)
    assert decision.passed is True


def test_max_logp_hard_rejects() -> None:
    """P0-B：expert_redlines.max_logp_hard 生效。"""
    cfg = load_config(mode="offline")
    # 压低红线 LogP，使 Ro5 已过但红线拒
    for step in cfg.filter_steps.get("steps", []):
        if step.get("id") == "lipinski_ro5":
            step.setdefault("params", {})["max_logp"] = 10.0
        if step.get("id") == "expert_redlines":
            step.setdefault("params", {})["max_logp_hard"] = 3.0
    # biphenyl 类适中 LogP 常 >3
    rec = _record_from_smiles("LOGP_HARD", "c1ccc(cc1)c2ccccc2")
    assert rec.logp > 3.0
    decision = apply_hard_filters(rec, cfg)
    assert decision.passed is False
    assert "expert_redlines" in decision.step_codes
    assert "LogP" in decision.reason


def test_sample_pass_rate_statable() -> None:
    cfg = load_config(mode="offline")
    records = parse_sdf(SAMPLE_SDF)
    decisions = [apply_hard_filters(r, cfg) for r in records]
    passed = sum(1 for d in decisions if d.passed)
    rate = passed / len(records)
    assert 0.0 < rate <= 1.0
    assert passed >= 1
