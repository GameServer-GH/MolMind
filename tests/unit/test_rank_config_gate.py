"""5.3：故意改坏权重时 GoldSet 门禁应红。"""

from __future__ import annotations

from packages.goldset import load_goldset
from services.eval_harness import run_goldset_harness
from services.pipeline.config_loader import load_config


def test_default_config_gate_passes() -> None:
    cfg = load_config(mode="offline")
    result = run_goldset_harness(cfg, load_goldset())
    assert result.passed, result.messages


def test_broken_tox_gate_fails_harness() -> None:
    cfg = load_config(mode="offline")
    # 故意放宽：假阳性几乎不可能触发 tox 失败断言
    cfg.raw["gates"]["tox_soft"] = 0.99
    cfg.raw["gates"]["tox_hard"] = 1.0
    result = run_goldset_harness(cfg, load_goldset())
    assert result.passed is False
    assert any("FAIL FP" in m for m in result.messages)
