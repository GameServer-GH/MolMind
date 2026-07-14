"""GoldSet：阳性 / 假阳性 / 阴性对照加载与相似计算。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from rdkit import Chem

from packages.chem_core import morgan_fp, tanimoto

ROOT = Path(__file__).resolve().parents[2]
GOLDSET_DIR = ROOT / "data" / "goldset"


@dataclass
class GoldCase:
    name: str
    role: str
    smiles: str
    cas: str | None
    expected: dict[str, Any]
    fp_bits: Any
    inchikey: str = ""


@dataclass
class GoldSet:
    positives: list[GoldCase]
    false_positives: list[GoldCase]
    negatives: list[GoldCase]

    def all_cases(self) -> list[GoldCase]:
        return [*self.positives, *self.false_positives, *self.negatives]


def _load_cases(path: Path) -> list[GoldCase]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    cases: list[GoldCase] = []
    for item in data.get("cases", []):
        smiles = item.get("smiles")
        if not smiles:
            continue
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            continue
        try:
            inchikey = Chem.MolToInchiKey(mol)
        except Exception:
            inchikey = ""
        cases.append(
            GoldCase(
                name=item["name"],
                role=item.get("role", "unknown"),
                smiles=smiles,
                cas=item.get("cas"),
                expected=item.get("expected") or {},
                fp_bits=morgan_fp(mol),
                inchikey=inchikey or "",
            )
        )
    return cases


def load_goldset(directory: Path | None = None) -> GoldSet:
    root = directory or GOLDSET_DIR
    # 合并经典阳性 + NAFLDkb 扩展阳性（去重 by name）
    positives = _load_cases(root / "positives.yaml")
    seen = {c.name.lower() for c in positives}
    for extra in _load_cases(root / "positives_nafld.yaml"):
        if extra.name.lower() in seen:
            continue
        positives.append(extra)
        seen.add(extra.name.lower())
    return GoldSet(
        positives=positives,
        false_positives=_load_cases(root / "false_positives.yaml"),
        negatives=_load_cases(root / "negatives.yaml"),
    )


def max_similarity(fp, cases: list[GoldCase]) -> tuple[float, str | None]:
    best = 0.0
    best_name: str | None = None
    for case in cases:
        sim = tanimoto(fp, case.fp_bits)
        if sim > best:
            best = sim
            best_name = case.name
    return best, best_name
