"""services.eval_harness：GoldSet 回归必绿。"""

from __future__ import annotations

from packages.goldset import load_goldset
from services.eval_harness import run_goldset_harness
from services.pipeline.config_loader import load_config


def test_goldset_harness_passes_offline() -> None:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    result = run_goldset_harness(cfg, gold)
    assert result.passed, result.messages
