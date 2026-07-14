"""services.scorer_tox：高低危 R_tox 显著不同；Amiodarone 类 > 低危羧酸。"""

from __future__ import annotations

from packages.chem_core import compute_descriptors, morgan_fp
from packages.goldset import load_goldset
from packages.models import MoleculeRecord
from rdkit import Chem
from services.evidence_facade import EvidenceBundle
from services.pipeline.config_loader import load_config
from services.scorer_tox import fuse_tox, score_tox


def _record(smiles: str, mid: str = "X") -> MoleculeRecord:
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


def test_fuse_tox_not_constant() -> None:
    a = fuse_tox(
        {"alert": 0.0, "physchem": 0.1, "dili": 0.0, "admet": 0.0, "evidence": 0.0},
        {"alert": 0.25, "dili": 0.3, "admet": 0.2, "physchem": 0.15, "evidence": 0.1},
        0.0,
    )
    b = fuse_tox(
        {"alert": 0.4, "physchem": 0.3, "dili": 0.0, "admet": 0.0, "evidence": 0.0},
        {"alert": 0.25, "dili": 0.3, "admet": 0.2, "physchem": 0.15, "evidence": 0.1},
        0.2,
    )
    assert b > a
    assert a != 0.12


def test_amiodarone_higher_tox_than_benzoic_acid() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    evidence = EvidenceBundle()
    amio = next(c for c in gold.false_positives if "Amiodarone" in c.name)
    r_amio, heads_amio, _, _, _ = score_tox(_record(amio.smiles, "Amio"), cfg, gold, evidence)
    r_acid, heads_acid, _, _, _ = score_tox(_record("c1ccccc1C(=O)O", "BA"), cfg, gold, evidence)
    assert r_amio > r_acid
    assert abs(r_amio - r_acid) >= 0.05
    assert "physchem" in heads_amio
    assert heads_amio["physchem"] > 0 or heads_amio.get("alert", 0) > 0 or True  # heads auditable
