"""B1：机制模板含通路白名单 / 证据 ID / 救援实验；不改排名。"""

from __future__ import annotations

from pathlib import Path

from packages.chem_core import compute_descriptors, morgan_fp
from packages.goldset import load_goldset
from packages.models import Attribution, MoleculeRecord
from rdkit import Chem
from services.evidence_facade import EvidenceBundle
from services.mechanism import render_mechanism_markdown
from services.pipeline import load_config, screen_sdf
from services.ranker import score_molecule

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SDF = ROOT / "data" / "sample.sdf"


def test_b1_mechanism_has_pathway_and_rescue(tmp_path: Path) -> None:
    cfg = load_config(mode="offline", seed=42)
    result = screen_sdf(SAMPLE_SDF, cfg=cfg, top_n=3)
    csv_before = result.to_csv_text()
    path = tmp_path / "mech.md"
    render_mechanism_markdown(result.top_molecules, path)
    text = path.read_text(encoding="utf-8")
    assert "假设通路" in text
    assert "通路分组一览" in text
    assert "统一实验验证协议" in text
    assert "降脂相关证据 ID" in text
    assert "救援" in text
    assert "活力 >=80%" in text
    assert "nafld_pathways.yaml" in text
    assert "有效命中" in text
    assert any(x in text for x in ("`DNL`", "`FAO`", "`PPAR`", "`FXR`", "`AMPK`", "`THR`"))
    assert csv_before == result.to_csv_text()


def test_b1_positive_maps_to_pathway(tmp_path: Path) -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    oca = next(c for c in gold.positives if c.name == "Obeticholic Acid")
    desc = compute_descriptors(oca.smiles)
    assert desc is not None
    mol = Chem.MolFromSmiles(oca.smiles)
    assert mol is not None
    record = MoleculeRecord(
        molecule_id="OCA",
        smiles=oca.smiles,
        inchikey=oca.inchikey,
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
    scored = score_molecule(record, cfg, gold, EvidenceBundle())
    scored.attributions.append(
        Attribution("evidence", "chembl_lipid_v1", value=0.8, evidence_id="chembl:TEST123")
    )
    path = tmp_path / "oca.md"
    render_mechanism_markdown([scored], path)
    text = path.read_text(encoding="utf-8")
    assert "`FXR`" in text
    assert "chembl:TEST123" in text
    assert "不捏造" not in text or "chembl:TEST123" in text
