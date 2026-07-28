"""services.pipeline：sample 端到端 TopN；同 seed Jaccard=1；无 SI 列。"""

from __future__ import annotations

from pathlib import Path

from plugins.molmind_core.scientific.evidence_facade.bundle import EvidenceBundle
from plugins.molmind_core.scientific.pipeline.config_loader import ALGORITHM_PATHS
from services.pipeline import CSV_COLUMNS, load_config, run_pipeline, screen_sdf

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SDF = ROOT / "data" / "sample.sdf"


def test_pipeline_sample_topn_csv(tmp_path: Path) -> None:
    out = tmp_path / "nomination_top10.csv"
    result = run_pipeline(SAMPLE_SDF, out, mode="offline", top_n=10)
    assert result.output_count >= 1
    assert out.is_file()
    header = out.read_text(encoding="utf-8-sig").splitlines()[0]
    assert header == ",".join(CSV_COLUMNS)
    assert "SI" not in header
    assert "EC50" not in header
    assert "CC50" not in header
    manifest = out.with_suffix(".run_manifest.json")
    robustness = out.with_suffix(".rank_robustness.json")
    resources = out.with_suffix(".hepg2_ffa_resources.json")
    assert manifest.is_file() and robustness.is_file() and resources.is_file()
    import json

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["input"]["sha256"]
    assert payload["config_hash"] == result.config.config_hash
    assert payload["assumption_policy_version"] == "screening-assumptions-v2"
    assert payload["rdkit_version"]
    assert payload["artifacts"][out.name]
    assert resources.name in payload["artifacts"]
    resource_payload = json.loads(resources.read_text(encoding="utf-8"))
    assert resource_payload["ranking_effect"] == "none"
    assert resource_payload["dual_endpoint_model_available"] is False
    assert resource_payload["resource_counts"]["total"] == 6
    assert result.diagnostics.scientific_validation_status.startswith("not_available")
    assert [m.final_score for m in result.top_molecules] == sorted(
        (m.final_score for m in result.top_molecules), reverse=True
    )
    assert not any("0.2000 < 0.2000" in note for note in result.diagnostics.notes)


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


def test_pipeline_parse_logs_explain_slowness_and_progress() -> None:
    cfg = load_config(mode="offline", seed=42)
    result = screen_sdf(SAMPLE_SDF, cfg=cfg, top_n=5)
    zh_logs = [
        entry["message"]
        for entry in result.logs
        if entry.get("lang") == "zh"
    ]
    joined = "\n".join(zh_logs)
    assert "开始解析 SDF 文件" in joined
    assert "解析耗时主要来自逐分子化学计算" in joined
    assert "互变异构规范化" in joined
    assert "单线程" in joined
    assert "无特征缓存" in joined
    assert "解析完成" in joined
    assert "耗时" in joined
    assert any("预估约" in line and "条记录" in line for line in zh_logs)


def test_explicit_live_authorization_cannot_change_same_run_ranking(monkeypatch) -> None:
    calls: list[bool] = []

    class FrozenOnlyFacade:
        def __init__(self, _cfg):
            pass

        def query(self, *, allow_live=False, **_identity):
            calls.append(bool(allow_live))
            return EvidenceBundle()

        def finalize_degraded_flags(self, *, any_hit):
            assert any_hit is False

    monkeypatch.setattr(
        "plugins.molmind_core.scientific.pipeline.runner.EvidenceFacade",
        FrozenOnlyFacade,
    )
    offline = screen_sdf(
        SAMPLE_SDF,
        cfg=load_config(mode="auto", allow_live=False),
        top_n=5,
    )
    requested = screen_sdf(
        SAMPLE_SDF,
        cfg=load_config(mode="auto", allow_live=True),
        top_n=5,
    )

    assert calls and all(value is False for value in calls)
    assert [row.molecule_id for row in requested.top_molecules] == [
        row.molecule_id for row in offline.top_molecules
    ]
    assert [row.final_score for row in requested.top_molecules] == [
        row.final_score for row in offline.top_molecules
    ]
    assert any(
        "same_run_live_scoring=blocked" in entry.get("message", "")
        for entry in requested.logs
    )


def test_config_hash_algorithm_boundary_uses_canonical_plugin_files() -> None:
    assert ALGORITHM_PATHS
    assert all(not path.startswith("services/") for path in ALGORITHM_PATHS)
    assert all((ROOT / path).is_file() for path in ALGORITHM_PATHS)
    assert "plugins/molmind_core/scientific/pipeline/runner.py" in ALGORITHM_PATHS
    assert "plugins/molmind_core/scientific/evidence_facade/facade.py" in ALGORITHM_PATHS
