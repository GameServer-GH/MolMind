#!/usr/bin/env python3
"""Build offline DILIrank ↔ library identity map (no network)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.evidence_facade.dilirank_gate import (
    DEFAULT_EPA_MAPPING,
    DEFAULT_IDENTITY_PATH,
    DEFAULT_OFFICIAL_CSV,
    DEFAULT_PROCESSED_IDENTITY_PATH,
    DEFAULT_REFERENCE_CSV,
    write_identity_jsonl,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_IDENTITY_PATH,
        help="primary identity_mapped.jsonl path (tracked under data/reference)",
    )
    parser.add_argument(
        "--also-processed",
        action="store_true",
        help="also write gitignored processed copy",
    )
    parser.add_argument("--official-csv", type=Path, default=DEFAULT_OFFICIAL_CSV)
    parser.add_argument("--reference-csv", type=Path, default=DEFAULT_REFERENCE_CSV)
    parser.add_argument("--epa-mapping", type=Path, default=DEFAULT_EPA_MAPPING)
    args = parser.parse_args()
    summary = write_identity_jsonl(
        args.output,
        official_csv=args.official_csv,
        reference_csv=args.reference_csv,
        epa_mapping=args.epa_mapping,
    )
    if args.also_processed:
        secondary = write_identity_jsonl(
            DEFAULT_PROCESSED_IDENTITY_PATH,
            official_csv=args.official_csv,
            reference_csv=args.reference_csv,
            epa_mapping=args.epa_mapping,
        )
        summary["processed_copy"] = secondary
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
