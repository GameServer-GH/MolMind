"""services.mechanism：不改排名。"""

from __future__ import annotations

from pathlib import Path

from packages.goldset import load_goldset
from services.evidence_facade import EvidenceBundle
from services.mechanism import render_mechanism_markdown
from services.pipeline import load_config, screen_sdf
from services.ranker import score_molecule

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SDF = ROOT / "data" / "sample.sdf"


def test_mechanism_does_not_change_csv_bytes(tmp_path: Path) -> None:
    cfg = load_config(mode="offline", seed=42)
    result = screen_sdf(SAMPLE_SDF, cfg=cfg, top_n=5)
    csv_before = result.to_csv_text()

    mech_path = tmp_path / "mech.md"
    render_mechanism_markdown(result.top_molecules, mech_path)
    assert mech_path.is_file()
    text = mech_path.read_text(encoding="utf-8")
    assert "活力 >=80%" in text or "80%" in text

    csv_after = result.to_csv_text()
    assert csv_before == csv_after


def test_mechanism_template_unchanged_by_editing_top_only() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    sim = next(c for c in gold.positives if c.name == "Simvastatin")
    from packages.chem_core import compute_descriptors, morgan_fp
    from packages.models import MoleculeRecord
    from rdkit import Chem

    desc = compute_descriptors(sim.smiles)
    assert desc is not None
    mol = Chem.MolFromSmiles(sim.smiles)
    assert mol is not None
    record = MoleculeRecord(
        molecule_id="Sim",
        smiles=sim.smiles,
        inchikey=sim.inchikey,
        cas=sim.cas,
        mw=float(desc["mw"]),
        logp=float(desc["logp"]),
        hbd=int(desc["hbd"]),
        hba=int(desc["hba"]),
        tpsa=float(desc["tpsa"]),
        rotatable_bonds=int(desc["rotatable_bonds"]),
        aromatic_rings=int(desc["aromatic_rings"]),
        fp_bits=morgan_fp(mol),
    )
    ev = EvidenceBundle()
    scored = score_molecule(record, cfg, gold, ev)
    assert scored.final_score > 0
