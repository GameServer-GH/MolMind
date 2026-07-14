"""CSV 导出列契约。"""

from __future__ import annotations

from pathlib import Path

from services.pipeline import CSV_COLUMNS, run_pipeline

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SDF = ROOT / "data" / "sample.sdf"


def test_csv_columns_include_run_metadata(tmp_path: Path) -> None:
    out = tmp_path / "out.csv"
    run_pipeline(SAMPLE_SDF, out, mode="auto", top_n=5)
    header = out.read_text(encoding="utf-8").splitlines()[0]
    assert header == ",".join(CSV_COLUMNS)
    assert "run_mode" in CSV_COLUMNS
    assert "config_hash" in CSV_COLUMNS
    assert "degraded_channels" in CSV_COLUMNS
    assert "SI" not in CSV_COLUMNS
    assert "EC50" not in CSV_COLUMNS
