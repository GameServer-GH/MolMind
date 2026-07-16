"""证据融合：有/无证据消融与无命中权重归一。"""

from __future__ import annotations

from packages.chem_core import compute_descriptors, morgan_fp
from packages.goldset import load_goldset
from packages.models import EvidenceHit, MoleculeRecord
from rdkit import Chem
import pytest
from services.evidence_facade import EvidenceBundle
from services.pipeline.config_loader import load_config
from services.ranker import score_molecule
from services.scorer_lipid import score_lipid


def _simvastatin_record() -> MoleculeRecord:
    gold = load_goldset()
    sim = next(c for c in gold.positives if c.name == "Simvastatin")
    desc = compute_descriptors(sim.smiles)
    assert desc is not None
    mol = Chem.MolFromSmiles(sim.smiles)
    assert mol is not None
    return MoleculeRecord(
        molecule_id="Sim",
        smiles=sim.smiles,
        inchikey=Chem.MolToInchiKey(mol) or "",
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


def test_with_evidence_raises_lipid_vs_without() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    record = _simvastatin_record()

    empty = EvidenceBundle()
    with_ev = EvidenceBundle(
        lipid=[
            EvidenceHit(
                adapter_id="chembl_lipid_v1",
                query_type="lipid",
                score=0.75,
                confidence=0.8,
                evidence_id="chembl:TEST:lipid",
            )
        ]
    )

    s_empty, _, _, _ = score_lipid(record, cfg, gold, empty)
    s_with, parts_with, _, rationale_with = score_lipid(record, cfg, gold, with_ev)

    assert s_with > s_empty
    assert parts_with["evidence"] == pytest.approx(0.75)
    assert "证据分" in rationale_with


def test_no_hit_weight_normalization() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    record = _simvastatin_record()
    empty = EvidenceBundle()

    s_lipid, parts, _, _ = score_lipid(record, cfg, gold, empty)
    fuse = cfg.lipid_fuse
    active_sum = fuse["rule"] + fuse["positive_similarity"]
    expected = (
        fuse["rule"] * parts["rule"] + fuse["positive_similarity"] * parts["positive_similarity"]
    ) / active_sum

    assert parts["evidence"] == 0.0
    assert parts["ml"] == 0.0
    assert s_lipid == pytest.approx(expected, rel=1e-3)


def test_evidence_ablation_changes_final_score() -> None:
    cfg = load_config(mode="offline")
    cfg.raw["gates"]["tox_nomination_max"] = cfg.raw["gates"]["tox_hard"]
    gold = load_goldset()
    record = _simvastatin_record()

    empty = EvidenceBundle()
    with_ev = EvidenceBundle(
        lipid=[
            EvidenceHit(
                adapter_id="chembl_lipid_v1",
                query_type="lipid",
                score=0.8,
                confidence=0.85,
                evidence_id="chembl:ABL:lipid",
            )
        ],
        tox=[
            EvidenceHit(
                adapter_id="pubchem_tox_v1",
                query_type="tox",
                score=0.1,
                confidence=0.5,
                evidence_id="pubchem:ABL:ghs",
            )
        ],
    )

    scored_empty = score_molecule(record, cfg, gold, empty)
    scored_with = score_molecule(record, cfg, gold, with_ev)

    assert scored_with.lipid_score > scored_empty.lipid_score
    assert scored_with.conf_e > scored_empty.conf_e
    assert scored_with.final_score != scored_empty.final_score
