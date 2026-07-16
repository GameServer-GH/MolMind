"""services.scorer_lipid：Simvastatin > 无关骨架；无证据时权重归一。"""

from __future__ import annotations

from packages.chem_core import compute_descriptors, morgan_fp
from packages.goldset import load_goldset
from packages.models import MoleculeRecord
from rdkit import Chem
from services.evidence_facade import EvidenceBundle
from services.pipeline.config_loader import load_config
from services.scorer_lipid import score_lipid


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


def test_simvastatin_higher_than_unrelated() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    evidence = EvidenceBundle()
    sim = next(c for c in gold.positives if c.name == "Simvastatin")
    s_sim, parts_sim, _, _ = score_lipid(_record(sim.smiles, "Sim"), cfg, gold, evidence)
    s_eth, parts_eth, _, _ = score_lipid(_record("CCO", "EtOH"), cfg, gold, evidence)
    assert s_sim > s_eth
    assert parts_sim["evidence"] == 0.0
    assert parts_eth["evidence"] == 0.0
    # 无证据时不应静默稀释：positive_similarity 应对 Simvastatin 显著
    assert parts_sim["positive_similarity"] > parts_eth["positive_similarity"]


def test_no_evidence_normalization_keeps_rule_weight() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    evidence = EvidenceBundle()
    sim = next(c for c in gold.positives if c.name == "Simvastatin")
    s_lipid, parts, _, rationale = score_lipid(_record(sim.smiles, "Sim"), cfg, gold, evidence)
    assert parts["evidence"] == 0.0
    assert parts["ml"] == 0.0
    assert float(cfg.lipid_fuse.get("ml", 0)) == 0.0
    assert "lipid_ml" not in cfg.degraded_channels
    assert "lipid_ml_missing" not in cfg.degraded_channels
    assert s_lipid > 0.3
    assert "阳性相似" in rationale or "药效团" in rationale


def test_lipid_ml_weight_zero_is_honest() -> None:
    """P0-A1：默认 lipid ML 权重为 0，不假装有通道。"""
    cfg = load_config(mode="offline")
    assert float(cfg.lipid_fuse.get("ml", 0)) == 0.0
