"""services.eval_harness：GoldSet 回归必绿；quality_gates.min_std_tox 可硬失败。"""

from __future__ import annotations

from packages.goldset import load_goldset
from services.eval_harness import run_goldset_harness
from services.pipeline.config_loader import load_config


def test_goldset_harness_passes_offline() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    result = run_goldset_harness(cfg, gold)
    assert result.passed, result.messages


def test_harness_does_not_treat_tox_dispersion_as_accuracy() -> None:
    """风险方差只能告警，不能伪装成独立科学性能门禁。"""
    cfg = load_config(mode="offline")
    cfg.raw.setdefault("quality_gates", {})["min_std_tox"] = 0.99
    gold = load_goldset()
    result = run_goldset_harness(cfg, gold)
    assert result.passed is True
    assert any("WARN TOX_STD" in m and "not treated as scientific accuracy" in m for m in result.messages)
