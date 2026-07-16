"""Deterministic binding between input, policy, ordered candidates and artifacts."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from packages.models import ScoreRecord


def canonical_selection(top: list[ScoreRecord]) -> list[dict[str, Any]]:
    """Return the minimal ordered candidate contract used for lineage hashes."""
    return [
        {
            "rank": rank,
            "molecule_id": molecule.molecule_id,
            "inchikey": molecule.inchikey,
            "canonical_smiles": molecule.smiles,
            "eligibility_status": molecule.eligibility_status,
            "final_score": format(float(molecule.final_score), ".12g"),
        }
        for rank, molecule in enumerate(top, start=1)
    ]


def selection_sha256(top: list[ScoreRecord]) -> str:
    payload = json.dumps(
        canonical_selection(top),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def deterministic_run_id(
    *,
    input_sha256: str,
    config_hash: str,
    selection_hash: str,
) -> str:
    payload = f"{input_sha256}\0{config_hash}\0{selection_hash}".encode("utf-8")
    return f"mm-{hashlib.sha256(payload).hexdigest()[:24]}"

