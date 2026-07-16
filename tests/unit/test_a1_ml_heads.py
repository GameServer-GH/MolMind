"""A1：本地 DILI/ADMET ML 头接线与门禁。"""

from __future__ import annotations

import json
from pathlib import Path

from packages.chem_core import compute_descriptors, morgan_fp
from packages.goldset import load_goldset
from packages.ml_optional import clear_heads_cache, load_optional_heads_bundle
from packages.models import MoleculeRecord
from rdkit import Chem
from services.evidence_facade.bundle import EvidenceBundle
from services.pipeline.config_loader import ROOT, load_config
from services.ranker import score_molecule
from services.scorer_tox import score_tox

MODELS_DIR = ROOT / "data" / "models"


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


def test_manifest_models_exist() -> None:
    manifest = json.loads((ROOT / "configs" / "model_manifest.json").read_text(encoding="utf-8"))
    assert manifest.get("models"), "A1 要求交付含本地模型条目"
    for entry in manifest["models"]:
        path = ROOT / entry["path"]
        assert path.is_file(), f"missing model file: {path}"


def test_knn_predicts_amiodarone_high() -> None:
    clear_heads_cache()
    manifest = json.loads((ROOT / "configs" / "model_manifest.json").read_text(encoding="utf-8"))
    bundle = load_optional_heads_bundle(manifest, model_dir=ROOT)
    assert not bundle.skipped
    amio = next(c for c in load_goldset().false_positives if "Amiodarone" in c.name)
    mol = Chem.MolFromSmiles(amio.smiles)
    pred = bundle.predict(mol)
    assert pred.skipped is False
    assert pred.dili >= 0.7
    assert pred.admet >= 0.5


def test_ml_raises_fp_tox_vs_baseline() -> None:
    """启用 ML 后，GoldSet 假阳性 R_tox 不得低于仅 alert+physchem 基线。"""
    clear_heads_cache()
    gold = load_goldset()
    amio = next(c for c in gold.false_positives if "Amiodarone" in c.name)
    rec = _record(amio.smiles, "Amio")

    cfg_ml = load_config(mode="offline")
    assert cfg_ml.ml_enabled is True
    r_ml, heads_ml, *_ = score_tox(rec, cfg_ml, gold, EvidenceBundle())

    cfg_off = load_config(mode="offline")
    cfg_off.raw["ml"]["enabled"] = False
    # bypass property: ml_enabled reads raw + manifest
    assert cfg_off.ml_enabled is False
    r_base, heads_base, *_ = score_tox(rec, cfg_off, gold, EvidenceBundle())

    assert heads_ml.get("dili", 0) > 0
    assert r_ml >= r_base
    assert "dili_ml_missing" not in cfg_ml.degraded_channels
    assert "dili_ml" not in cfg_ml.degraded_channels


def test_ml_disabled_marks_degraded() -> None:
    clear_heads_cache()
    cfg = load_config(mode="offline")
    cfg.raw["ml"]["enabled"] = False
    gold = load_goldset()
    rec = _record("CCO", "EtOH")
    score_tox(rec, cfg, gold, EvidenceBundle())
    assert "dili_ml_missing" in cfg.degraded_channels
    assert "admet_ml_missing" in cfg.degraded_channels


def test_benign_notes_ml_no_neighbor_stats() -> None:
    clear_heads_cache()
    cfg = load_config(mode="offline")
    gold = load_goldset()
    rec = _record("CCO", "EtOH")
    scored = score_molecule(rec, cfg, gold, EvidenceBundle())
    assert scored.tox_heads.get("dili", 0.0) < 0.5
    # 有模型时乙醇多半无邻居：计入 run 统计，但不写 degraded no_neighbor
    assert cfg.ml_predict_calls >= 1
    note = cfg.finalize_ml_run_stats()
    assert note is not None and "ml_neighbors" in note
    assert "dili_ml_no_neighbor" not in cfg.degraded_channels
