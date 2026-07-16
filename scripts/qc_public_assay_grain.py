#!/usr/bin/env python3
"""Build endpoint-QC tables from public processed assay-grain imports."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.public_data.qc import run_assay_grain_qc


def main() -> int:
    report = run_assay_grain_qc()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
