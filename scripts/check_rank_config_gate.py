#!/usr/bin/env python3
"""改 rank_weights 后的门禁：GoldSet harness 必须通过。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.goldset import load_goldset
from services.eval_harness import run_goldset_harness
from services.pipeline.config_loader import load_config


def main() -> int:
    cfg = load_config(mode="offline")
    gold = load_goldset()
    result = run_goldset_harness(cfg, gold)
    for msg in result.messages:
        print(msg)
    if not result.passed:
        print("GATE FAIL: GoldSet harness did not pass", file=sys.stderr)
        return 1
    print("GATE PASS: GoldSet harness OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
