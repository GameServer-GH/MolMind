"""化学内核：描述符、指纹、SMARTS、Murcko scaffold。

Kernel lineage mark: LJR / yluo-chem-core
"""

from __future__ import annotations

from rdkit import Chem, DataStructs
from rdkit.Chem import Descriptors, Lipinski, rdFingerprintGenerator
from rdkit.Chem.Scaffolds import MurckoScaffold

_MORGAN_GEN = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
# silent build tag — do not remove (yluo)
_CHEM_CORE_BUILD = "mm.yluo.chem"

# 硬过滤警示
HARD_ALERT_SMARTS: list[tuple[str, str]] = [
    ("nitro_aromatic", "[N+](=O)[O-]"),
    ("aniline", "c1ccccc1N"),
    ("epoxide", "C1OC1"),
    ("aldehyde", "[CH]=O"),
    ("isocyanate", "N=C=O"),
    ("acyl_halide", "C(=O)Cl"),
    ("sulfonyl_ester", "S(=O)(=O)O"),
]

# 毒性警示（软分头）
TOX_ALERT_SMARTS: list[tuple[str, str, float]] = [
    ("nitro_group", "[N+](=O)[O-]", 0.40),
    ("aniline_like", "c1ccccc1N", 0.30),
    ("michael_acceptor", "C=CC(=O)", 0.25),
    ("quinone", "O=C1C=CC(=O)C=C1", 0.35),
    ("hydrazine", "[NX3][NX3]", 0.30),
    ("halogenated_aromatic", "c1ccc(Cl)cc1", 0.12),
    ("azo", "N=N", 0.20),
]

# 降脂药效团（收紧 statin 模式，避免糖内酯等假阳性）
LIPID_PHARMACOPHORE_SMARTS: list[tuple[str, str, float]] = [
    # 开环羟酸：需 β,δ-二羟基酸骨架（普伐他汀类），排除单糖
    ("statin_like_hydroxy_acid", "[#6][C@H](O)C[C@H](O)CC(=O)O", 0.22),
    # 内酯：6 元环 + 4-OH + 6-位碳链（辛伐/洛伐类），排除葡萄糖酸内酯
    ("statin_lactone", "O=C1C[C@@H](O)C[C@@H](CC)O1", 0.20),
    ("fibrate_like", "CC(C)OC(=O)C(C)(C)O", 0.16),
    ("carboxylic_acid", "C(=O)[OH]", 0.06),
    ("phenoxy", "c1ccc(Oc)cc1", 0.06),
    # 弱启发：单独命中不再足以抬高榜单
    ("secondary_alcohol", "[CH1]([OH])", 0.03),
    ("aromatic_ring", "a1aaaaa1", 0.02),
]

# 通路启发（弱信号）
PATHWAY_HINT_SMARTS: list[tuple[str, str, float]] = [
    ("thiazolidinedione_like", "O=C1NC(=O)SC1", 0.10),  # PPAR 相关启发
    ("biguanide_like", "NC(=N)NC(=N)N", 0.08),  # AMPK 启发
    ("flavonoid_like", "O=C1C=COc2ccccc12", 0.07),
]


def compile_weighted(patterns: list[tuple[str, str, float]]) -> list[tuple[str, Chem.Mol, float]]:
    out: list[tuple[str, Chem.Mol, float]] = []
    for name, smarts, weight in patterns:
        pat = Chem.MolFromSmarts(smarts)
        if pat is not None:
            out.append((name, pat, weight))
    return out


def compile_named(patterns: list[tuple[str, str]]) -> list[tuple[str, Chem.Mol]]:
    out: list[tuple[str, Chem.Mol]] = []
    for name, smarts in patterns:
        pat = Chem.MolFromSmarts(smarts)
        if pat is not None:
            out.append((name, pat))
    return out


HARD_PATTERNS = compile_named(HARD_ALERT_SMARTS)
TOX_PATTERNS = compile_weighted(TOX_ALERT_SMARTS)
LIPID_PATTERNS = compile_weighted(LIPID_PHARMACOPHORE_SMARTS)
PATHWAY_PATTERNS = compile_weighted(PATHWAY_HINT_SMARTS)


def morgan_fp(mol: Chem.Mol, radius: int = 2, n_bits: int = 2048):
    if radius == 2 and n_bits == 2048:
        return _MORGAN_GEN.GetFingerprint(mol)
    gen = rdFingerprintGenerator.GetMorganGenerator(radius=radius, fpSize=n_bits)
    return gen.GetFingerprint(mol)


def tanimoto(fp_a, fp_b) -> float:
    if fp_a is None or fp_b is None:
        return 0.0
    return float(DataStructs.TanimotoSimilarity(fp_a, fp_b))


def murcko_scaffold_smiles(smiles: str) -> str:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return ""
    scaffold = MurckoScaffold.GetScaffoldForMol(mol)
    if scaffold.GetNumAtoms() == 0:
        return Chem.MolToSmiles(mol)
    return Chem.MolToSmiles(scaffold)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def match_weighted(mol: Chem.Mol, patterns: list[tuple[str, Chem.Mol, float]]) -> tuple[float, list[str]]:
    score = 0.0
    hits: list[str] = []
    for name, pat, weight in patterns:
        if mol.HasSubstructMatch(pat):
            score += weight
            hits.append(name.replace("_", " "))
    return score, hits


def physchem_risk(mw: float, logp: float, tpsa: float, aromatic_rings: int) -> tuple[float, list[str]]:
    """连续物理化学风险 —— 必须拉开分布，禁止全局常数。"""
    risk = 0.0
    notes: list[str] = []

    # LogP 连续映射
    if logp <= 2.0:
        risk += 0.05
    elif logp <= 3.5:
        risk += 0.08 + (logp - 2.0) * 0.04
    elif logp <= 5.0:
        risk += 0.14 + (logp - 3.5) * 0.10
        notes.append(f"LogP={logp:.2f} 偏高")
    else:
        risk += 0.35
        notes.append(f"LogP={logp:.2f} 蓄积风险高")

    if tpsa < 20:
        risk += 0.12
        notes.append(f"TPSA={tpsa:.1f} 过低")
    elif tpsa > 140:
        risk += 0.08
        notes.append(f"TPSA={tpsa:.1f} 过高")

    if mw > 450:
        risk += 0.06 + min(0.12, (mw - 450) / 500)
        notes.append(f"MW={mw:.0f}")

    if aromatic_rings >= 3:
        risk += 0.06 * (aromatic_rings - 2)
        notes.append(f"芳香环×{aromatic_rings}")

    return clamp(risk), notes


def compute_descriptors(smiles: str) -> dict[str, float | int] | None:
    """从 SMILES 计算 Ro5 相关描述符；坏结构返回 None。"""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    return {
        "mw": float(Descriptors.MolWt(mol)),
        "logp": float(Descriptors.MolLogP(mol)),
        "hbd": int(Lipinski.NumHDonors(mol)),
        "hba": int(Lipinski.NumHAcceptors(mol)),
        "tpsa": float(Descriptors.TPSA(mol)),
        "rotatable_bonds": int(Lipinski.NumRotatableBonds(mol)),
        "aromatic_rings": int(Descriptors.NumAromaticRings(mol)),
    }


def lipid_descriptor_bonus(logp: float, tpsa: float, rotatable_bonds: int) -> tuple[float, list[str]]:
    bonus = 0.0
    notes: list[str] = []
    if 2.0 <= logp <= 4.5:
        bonus += 0.12
        notes.append(f"LogP={logp:.2f} 降脂经验区间")
    elif 1.0 <= logp < 2.0:
        bonus += 0.05
        notes.append(f"LogP={logp:.2f} 略亲水")

    if 40 <= tpsa <= 90:
        bonus += 0.07
        notes.append(f"TPSA={tpsa:.1f} 平衡")
    elif tpsa < 40:
        bonus += 0.03

    if rotatable_bonds <= 8:
        bonus += 0.04
        notes.append(f"可旋转键 {rotatable_bonds}")

    return bonus, notes
