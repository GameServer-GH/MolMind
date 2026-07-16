"""CSV 导出列契约。"""

from __future__ import annotations

from pathlib import Path
import json

from services.pipeline import CSV_COLUMNS, run_pipeline

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SDF = ROOT / "data" / "sample.sdf"


def test_csv_columns_include_run_metadata(tmp_path: Path) -> None:
    out = tmp_path / "out.csv"
    run_pipeline(SAMPLE_SDF, out, mode="auto", top_n=5)
    header = out.read_text(encoding="utf-8-sig").splitlines()[0]
    assert header == ",".join(CSV_COLUMNS)
    assert "run_mode" in CSV_COLUMNS
    assert "config_hash" in CSV_COLUMNS
    assert "degraded_channels" in CSV_COLUMNS
    assert CSV_COLUMNS[:5] == ["排名", "化合物标识符", "降脂依据", "毒性判断", "排序理由"]
    assert "SI" not in CSV_COLUMNS
    assert "EC50" not in CSV_COLUMNS


def test_pipeline_writes_candidate_scores_and_evidence_ledger(tmp_path: Path) -> None:
    out = tmp_path / "out.csv"
    run_pipeline(SAMPLE_SDF, out, mode="offline", top_n=2, write_mechanism=False)
    scores = out.with_suffix(".candidate_scores.jsonl")
    ledger = out.with_suffix(".evidence_ledger.jsonl")
    citations = out.with_suffix(".citations.jsonl")
    selection_audit = out.with_suffix(".selection_audit.jsonl")
    assert scores.is_file()
    assert ledger.is_file()
    assert citations.is_file()
    assert selection_audit.is_file()
    score_rows = [json.loads(line) for line in scores.read_text(encoding="utf-8").splitlines()]
    ledger_rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    audit_rows = [
        json.loads(line) for line in selection_audit.read_text(encoding="utf-8").splitlines()
    ]
    assert score_rows
    assert ledger_rows
    assert all("scientific_status" in row for row in score_rows)
    assert all("query_status" in row for row in ledger_rows)
    assert any(row.get("evidence_type") for row in ledger_rows) or any(
        row.get("evidence_type") == "query_audit" for row in ledger_rows
    )
    assert any(row.get("outcome") == "selected" for row in audit_rows)
    assert all("selection_factors" in row for row in audit_rows)
