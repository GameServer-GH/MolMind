"""snapshot compact：同 key 保留最后一条。"""

from __future__ import annotations

import json
from pathlib import Path

from services.evidence_facade.snapshot_compact import compact_snapshot_jsonl


def test_compact_keeps_last_row(tmp_path: Path) -> None:
    path = tmp_path / "auto_cache.jsonl"
    rows = [
        {
            "inchikey": "AAA",
            "adapter_id": "chembl_lipid_v1",
            "query_type": "lipid",
            "score": 0.1,
            "confidence": 0.1,
            "evidence_id": "old",
        },
        {
            "inchikey": "AAA",
            "adapter_id": "chembl_lipid_v1",
            "query_type": "lipid",
            "score": 0.9,
            "confidence": 0.8,
            "evidence_id": "new",
        },
        {
            "inchikey": "BBB",
            "adapter_id": "pubchem_tox_v1",
            "query_type": "tox",
            "score": 0.2,
            "confidence": 0.3,
            "evidence_id": "tox1",
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    stats = compact_snapshot_jsonl(path, backup=True)
    assert stats.input_rows == 3
    assert stats.output_rows == 2
    assert stats.dropped_duplicates == 1
    kept = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    by_id = {r["evidence_id"]: r for r in kept}
    assert "new" in by_id
    assert "old" not in by_id
    assert path.with_suffix(".jsonl.bak").is_file()
