#!/usr/bin/env python3
"""从 data/reference/dilirank.csv 烘焙本地 DILI / ADMET k-NN 模型（无外网）。"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

from rdkit import Chem

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.ml_optional.knn_model import mol_on_bits  # noqa: E402


def _load_rows(csv_path: Path) -> list[dict]:
    rows: list[dict] = []
    with csv_path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            smiles = (row.get("smiles") or "").strip()
            if not smiles:
                continue
            mol = Chem.MolFromSmiles(smiles)
            if mol is None:
                continue
            score = float(row.get("score") or 0.0)
            rows.append(
                {
                    "name": (row.get("name") or "").strip(),
                    "score": score,
                    "smiles": smiles,
                    "concern": (row.get("concern") or "").strip(),
                    "on_bits": mol_on_bits(mol),
                }
            )
    return rows


def _write_model(path: Path, *, version: str, kind: str, entries: list[dict], **meta) -> None:
    payload = {
        "version": version,
        "kind": kind,
        "radius": 2,
        "n_bits": 2048,
        "k": int(meta.get("k", 3)),
        "sim_threshold": float(meta.get("sim_threshold", 0.40)),
        "source": "data/reference/dilirank.csv",
        "notes": meta.get("notes", ""),
        "entries": [
            {"name": e["name"], "score": e["score"], "on_bits": e["on_bits"]} for e in entries
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path} entries={len(entries)}")


def main() -> None:
    csv_path = ROOT / "data" / "reference" / "dilirank.csv"
    out_dir = ROOT / "data" / "models"
    rows = _load_rows(csv_path)
    if not rows:
        raise SystemExit(f"no usable rows in {csv_path}")

    # DILI：直接用标注 score（Most≈0.9 / Less≈0.55）
    _write_model(
        out_dir / "dili_knn_v1.json",
        version="dili_knn_v1",
        kind="dili_knn",
        entries=rows,
        k=3,
        sim_threshold=0.40,
        notes="ECFP4 k-NN over curated DILIrank subset; proxy for hepatotoxicity risk.",
    )

    # ADMET 代理：抬高 Most-DILI / curated-Most，压低 Less；作肝毒相关 ADMET 代理头
    admet_entries = []
    for e in rows:
        concern = e["concern"].lower()
        if "less" in concern:
            score = min(0.45, e["score"] * 0.7)
        elif "most" in concern:
            score = max(0.7, e["score"])
        else:
            score = e["score"]
        admet_entries.append({**e, "score": round(score, 4)})
    _write_model(
        out_dir / "admet_proxy_v1.json",
        version="admet_proxy_v1",
        kind="admet_proxy",
        entries=admet_entries,
        k=3,
        sim_threshold=0.42,
        notes="DILIrank-derived ADMET hepatotox proxy (not full ADMET suite).",
    )


if __name__ == "__main__":
    main()
