#!/usr/bin/env python3
"""审计 LIPID/PATHWAY SMARTS 在样本库上的命中分布（P1-A；只读，不改分）。"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rdkit import Chem

from packages.chem_core import LIPID_PATTERNS, PATHWAY_PATTERNS
from services.ingest import parse_sdf


def main() -> int:
    sdf = ROOT / "data" / "sample.sdf"
    if len(sys.argv) > 1:
        sdf = Path(sys.argv[1])
    records = parse_sdf(sdf)
    lipid_hits: Counter[str] = Counter()
    pathway_hits: Counter[str] = Counter()
    for rec in records:
        mol = Chem.MolFromSmiles(rec.smiles)
        if mol is None:
            continue
        for name, pat, _w in LIPID_PATTERNS:
            if mol.HasSubstructMatch(pat):
                lipid_hits[name] += 1
        for name, pat, _w in PATHWAY_PATTERNS:
            if mol.HasSubstructMatch(pat):
                pathway_hits[name] += 1
    print(f"molecules={len(records)} file={sdf}")
    print("LIPID_PATTERNS hits:")
    for k, v in lipid_hits.most_common():
        print(f"  {k}: {v}")
    print("PATHWAY_PATTERNS hits:")
    for k, v in pathway_hits.most_common():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
