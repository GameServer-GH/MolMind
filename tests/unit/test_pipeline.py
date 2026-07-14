"""services.pipeline：sample 端到端 TopN；同 seed Jaccard=1；无 SI 列。"""

from __future__ import annotations

from pathlib import Path

from services.pipeline import CSV_COLUMNS, load_config, run_pipeline, screen_sdf

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SDF = ROOT / "data" / "sample.sdf"


def test_pipeline_sample_topn_csv(tmp_path: Path) -> None:
    out = tmp_path / "nomination_top10.csv"
    result = run_pipeline(SAMPLE_SDF, out, mode="offline", top_n=10)
    assert result.output_count >= 1
    assert out.is_file()
    header = out.read_text(encoding="utf-8").splitlines()[0]
    assert header == ",".join(CSV_COLUMNS)
    assert "SI" not in header
    assert "EC50" not in header
    assert "CC50" not in header


def test_pipeline_deterministic_jaccard(tmp_path: Path) -> None:
    cfg = load_config(mode="offline", seed=42)
    r1 = screen_sdf(SAMPLE_SDF, cfg=cfg, top_n=10)
    r2 = screen_sdf(SAMPLE_SDF, cfg=load_config(mode="offline", seed=42), top_n=10)
    ids1 = {m.molecule_id for m in r1.top_molecules}
    ids2 = {m.molecule_id for m in r2.top_molecules}
    assert ids1 == ids2
    jaccard = len(ids1 & ids2) / len(ids1 | ids2) if ids1 or ids2 else 1.0
    assert jaccard == 1.0
    # CSV 字节级一致
    out1 = tmp_path / "a.csv"
    out2 = tmp_path / "b.csv"
    run_pipeline(SAMPLE_SDF, out1, mode="offline", top_n=10)
    run_pipeline(SAMPLE_SDF, out2, mode="offline", top_n=10)
    assert out1.read_text(encoding="utf-8") == out2.read_text(encoding="utf-8")
