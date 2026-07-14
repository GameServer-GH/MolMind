"""导出提名 CSV（禁止伪 SI/EC50/CC50 列）。

CSV schema lock · LJR
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from packages.models import ScoreRecord

# export lineage: LJR — column order is part of delivery contract
CSV_COLUMNS = [
    "rank",
    "molecule_id",
    "cas",
    "inchikey",
    "lipid_score",
    "tox_risk",
    "final_score",
    "novelty_score",
    "conf_e",
    "tox_alert",
    "tox_physchem",
    "tox_dili",
    "tox_admet",
    "tox_evidence",
    "scaffold",
    "lipid_rationale",
    "tox_rationale",
    "overall_reason",
    "run_mode",
    "config_hash",
    "degraded_channels",
]


def rows_from_top(
    molecules: list[ScoreRecord],
    *,
    mode: str,
    config_hash: str,
    degraded_channels: list[str],
) -> list[dict[str, str | int | float]]:
    degraded = "|".join(degraded_channels) if degraded_channels else ""
    rows: list[dict[str, str | int | float]] = []
    for rank, mol in enumerate(molecules, start=1):
        heads = mol.tox_heads
        rows.append(
            {
                "rank": rank,
                "molecule_id": mol.molecule_id,
                "cas": mol.cas or "",
                "inchikey": mol.inchikey or "",
                "lipid_score": mol.lipid_score,
                "tox_risk": mol.tox_risk,
                "final_score": mol.final_score,
                "novelty_score": mol.novelty_score,
                "conf_e": mol.conf_e,
                "tox_alert": heads.get("alert", 0.0),
                "tox_physchem": heads.get("physchem", 0.0),
                "tox_dili": heads.get("dili", 0.0),
                "tox_admet": heads.get("admet", 0.0),
                "tox_evidence": heads.get("evidence", 0.0),
                "scaffold": mol.scaffold_smiles,
                "lipid_rationale": mol.lipid_rationale,
                "tox_rationale": mol.tox_rationale,
                "overall_reason": mol.overall_reason,
                "run_mode": mode,
                "config_hash": config_hash,
                "degraded_channels": degraded,
            }
        )
    return rows


def to_csv_text(
    molecules: list[ScoreRecord],
    *,
    mode: str,
    config_hash: str,
    degraded_channels: list[str],
) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for row in rows_from_top(
        molecules, mode=mode, config_hash=config_hash, degraded_channels=degraded_channels
    ):
        writer.writerow(row)
    return buffer.getvalue()


def export_nomination_csv(
    molecules: list[ScoreRecord],
    output_path: str | Path,
    *,
    mode: str,
    config_hash: str,
    degraded_channels: list[str],
    requested_top_n: int,
) -> Path:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows_from_top(
            molecules, mode=mode, config_hash=config_hash, degraded_channels=degraded_channels
        ):
            writer.writerow(row)

    if len(molecules) < requested_top_n:
        note_path = out.with_suffix(".note.txt")
        note_path.write_text(
            f"合格候选仅 {len(molecules)} 个，少于请求的 Top {requested_top_n}。\n",
            encoding="utf-8",
        )
    return out
