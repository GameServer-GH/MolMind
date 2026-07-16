"""packages.chem_core：描述符、指纹、statin SMARTS 回归。"""

from __future__ import annotations

from rdkit import Chem

from packages.chem_core import (
    LIPID_PATTERNS,
    compute_descriptors,
    match_weighted,
    morgan_fp,
    murcko_scaffold_smiles,
    tanimoto,
)

# Simvastatin (开环酸形式近似；内酯形式用于 lactone SMARTS)
SIMVASTATIN_LACTONE = (
    "CCC(C)(C)C(=O)O[C@H]1C[C@@H](C)C=C2C=C[C@H](C)[C@H](CC[C@@H]3C[C@@H](O)CC(=O)O3)[C@@H]12"
)
# D-glucono-1,5-lactone — 糖内酯，不应命中 statin_lactone
GLUCONO_LACTONE = "O=C1O[C@H](CO)[C@@H](O)[C@H](O)[C@H]1O"
# 糖醛酸结合物：旧开链 SMARTS 会沿糖环误命中 statin_like_hydroxy_acid。
GLUCURONIDE = "Cc1cc(=O)oc2cc(O[C@@H]3OC(C(=O)O)[C@@H](O)[C@H](O)[C@H]3O)ccc12"
ACYCLIC_DIHYDROXY_ACID = "CC(O)CC(O)CC(=O)O"
ETHANOL = "CCO"
BAD_SMILES = "not_a_smiles_!!!"


def test_known_smiles_descriptors_in_range() -> None:
    desc = compute_descriptors(ETHANOL)
    assert desc is not None
    assert 40 < desc["mw"] < 55
    assert -1.0 <= desc["logp"] <= 1.0
    assert desc["hbd"] == 1
    assert desc["hba"] == 1
    assert desc["aromatic_rings"] == 0


def test_bad_smiles_returns_none() -> None:
    assert compute_descriptors(BAD_SMILES) is None
    assert murcko_scaffold_smiles(BAD_SMILES) == ""


def test_fingerprint_and_self_tanimoto() -> None:
    mol = Chem.MolFromSmiles(ETHANOL)
    assert mol is not None
    fp = morgan_fp(mol)
    assert fp is not None
    assert tanimoto(fp, fp) >= 0.99
    assert tanimoto(None, fp) == 0.0


def test_statin_smarts_does_not_hit_sugar_lactone() -> None:
    sugar = Chem.MolFromSmiles(GLUCONO_LACTONE)
    statin = Chem.MolFromSmiles(SIMVASTATIN_LACTONE)
    assert sugar is not None and statin is not None

    sugar_score, sugar_hits = match_weighted(sugar, LIPID_PATTERNS)
    statin_score, statin_hits = match_weighted(statin, LIPID_PATTERNS)

    assert "statin lactone" not in sugar_hits
    assert "statin like hydroxy acid" not in sugar_hits
    # 糖内酯最多可能命中弱启发；不得靠 statin 药效团抬分
    assert sugar_score < 0.15
    assert "statin lactone" in statin_hits or "statin like hydroxy acid" in statin_hits
    assert statin_score > sugar_score


def test_statin_open_chain_smarts_excludes_glucuronide_ring() -> None:
    glucuronide = Chem.MolFromSmiles(GLUCURONIDE)
    acyclic = Chem.MolFromSmiles(ACYCLIC_DIHYDROXY_ACID)
    assert glucuronide is not None and acyclic is not None
    _, glucuronide_hits = match_weighted(glucuronide, LIPID_PATTERNS)
    _, acyclic_hits = match_weighted(acyclic, LIPID_PATTERNS)
    assert "statin like hydroxy acid" not in glucuronide_hits
    assert "statin like hydroxy acid" in acyclic_hits
