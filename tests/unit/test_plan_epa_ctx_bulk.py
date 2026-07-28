from __future__ import annotations

import json
from pathlib import Path

from scripts.plan_epa_ctx_bulk import read_sdf


def test_bulk_reader_preserves_sdf_identity_fields() -> None:
    root = Path(__file__).resolve().parents[2]
    path = root / "data/T001 TargetMol现货产品22966.sdf"
    rows = read_sdf(path, 3)
    assert len(rows) == 3
    assert rows[0]["molecule_id"] == "T0002"
    assert rows[0]["cas"] == "2624-44-4"
    assert rows[0]["original_inchikey"]
    assert rows[0]["standardized_inchikey"]
    assert rows[0]["standardized_smiles"]
    assert rows[0]["standardization_steps"]
