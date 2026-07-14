"""regression：snapshot 加载后 auto 模式 pipeline 可跑且 index 非空。"""

from __future__ import annotations

from pathlib import Path

from services.evidence_facade import EvidenceFacade
from services.pipeline import load_config, screen_sdf

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SDF = ROOT / "data" / "sample.sdf"
SNAPSHOT_DIR = ROOT / "data" / "evidence_snapshot"


def test_auto_cache_index_non_empty() -> None:
    cfg = load_config(mode="auto")
    facade = EvidenceFacade(cfg, snapshot_dir=SNAPSHOT_DIR)
    assert len(facade._index) > 0


def test_offline_auto_with_snapshot_pipeline_runs() -> None:
    cfg = load_config(mode="auto")
    result = screen_sdf(SAMPLE_SDF, cfg=cfg, top_n=15)
    assert result.output_count >= 1
    assert result.input_count >= 1
    # snapshot 中已知分子（Simvastatin 类他汀 InChIKey）可被 facade 索引
    known_key = "PCZOHLXUXFIOCF-BXMDZJJMSA-N"
    facade = EvidenceFacade(cfg, snapshot_dir=SNAPSHOT_DIR)
    assert known_key in facade._index
    bundle = facade.query(inchikey=known_key, cas="75330-75-5", smiles="", allow_live=False)
    assert bundle.lipid_score > 0 or bundle.tox_score > 0
