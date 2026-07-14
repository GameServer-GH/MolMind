"""A2：NAFLDkb 扩阳性 + 通路表。"""

from __future__ import annotations

from packages.chem_core import compute_descriptors, morgan_fp, tanimoto
from packages.goldset import (
    infer_pathway_for_positive,
    load_goldset,
    load_nafld_pathways,
    max_similarity,
    pathway_by_id,
)
from packages.models import MoleculeRecord
from rdkit import Chem
from services.evidence_facade.bundle import EvidenceBundle
from services.pipeline.config_loader import load_config
from services.ranker import score_molecule


STATIN_NAMES = {"Simvastatin", "Lovastatin", "Fenofibrate"}


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


def test_nafld_positives_merged() -> None:
    gold = load_goldset()
    names = {c.name for c in gold.positives}
    assert "Simvastatin" in names
    assert "Pioglitazone" in names
    assert "Obeticholic Acid" in names
    assert "Metformin" in names
    non_statin = [c for c in gold.positives if c.name not in STATIN_NAMES]
    assert len(non_statin) >= 5


def test_pathways_whitelist_and_map() -> None:
    table = load_nafld_pathways()
    assert len(table["whitelist"]) >= 6
    ids = {p["id"] for p in table["whitelist"]}
    assert {"DNL", "FAO", "FXR", "AMPK", "PPAR"}.issubset(ids)
    fxr = pathway_by_id("FXR", table)
    assert fxr is not None
    assert "FXR" in fxr["targets"]
    pio = infer_pathway_for_positive("Pioglitazone", table)
    assert pio is not None
    assert pio["id"] == "PPAR"


def test_non_statin_positive_similarity_lifts_lipid() -> None:
    """库内分子若近似 NAFLD 非他汀阳性，S_lipid 的阳性相似分量应抬升。"""
    gold = load_goldset()
    pio = next(c for c in gold.positives if c.name == "Pioglitazone")
    # 自身作为查询：应命中 Pioglitazone
    sim, name = max_similarity(pio.fp_bits, gold.positives)
    assert name == "Pioglitazone"
    assert sim >= 0.99

    cfg = load_config(mode="offline")
    scored = score_molecule(_record(pio.smiles, "Pio"), cfg, gold, EvidenceBundle())
    assert scored.lipid_parts.get("positive_similarity", 0.0) >= 0.5
    assert scored.lipid_score >= 0.35


def test_main_evidence_freeze_unchanged() -> None:
    """A2 不改变定榜主证据口径。"""
    cfg = load_config(mode="offline")
    flags = cfg.evidence.get("adapter_flags") or {}
    assert flags.get("nafldkb_v1", {}).get("enabled") is False
    assert flags.get("nafldkb_v1", {}).get("ranking_weight", 0) == 0
    adapters = cfg.evidence.get("adapters") or []
    assert "chembl_lipid_v1" in adapters
    assert "pubchem_tox_v1" in adapters
    assert "nafldkb_v1" not in adapters
