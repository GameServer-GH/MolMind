"""packages.goldset：YAML 可解析；Amiodarone FP 非空；自身相似 ≥0.99。"""

from __future__ import annotations

from packages.chem_core import morgan_fp, tanimoto
from packages.goldset import load_goldset, max_similarity
from rdkit import Chem


def test_three_yaml_files_parse() -> None:
    gold = load_goldset()
    assert len(gold.positives) >= 1
    assert len(gold.false_positives) >= 1
    assert len(gold.negatives) >= 1


def test_amiodarone_fp_nonempty_and_self_similar() -> None:
    gold = load_goldset()
    amio = next((c for c in gold.false_positives if "Amiodarone" in c.name), None)
    assert amio is not None, "false_positives 应含 Amiodarone"
    assert amio.fp_bits is not None
    assert tanimoto(amio.fp_bits, amio.fp_bits) >= 0.99

    mol = Chem.MolFromSmiles(amio.smiles)
    assert mol is not None
    fp = morgan_fp(mol)
    sim, name = max_similarity(fp, gold.false_positives)
    assert sim >= 0.99
    assert name == amio.name


def test_positive_self_similarity() -> None:
    gold = load_goldset()
    case = gold.positives[0]
    sim, name = max_similarity(case.fp_bits, gold.positives)
    assert sim >= 0.99
    assert name == case.name


def test_assay_note_loaded_descriptive_only() -> None:
    """P1-B：assay_note 可加载，不参与相似计算语义。"""
    gold = load_goldset()
    sim = next(c for c in gold.positives if c.name == "Simvastatin")
    assert getattr(sim, "assay_note", "")
    assert "HepG2" in sim.assay_note or "hepg2" in sim.assay_note.lower()
