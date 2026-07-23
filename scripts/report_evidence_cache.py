#!/usr/bin/env python3
"""Inspect MolMind evidence query state without exposing cached payloads."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.evidence_gateway import EvidenceQueryCache  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        type=Path,
        default=ROOT / "data/public/cache/evidence_query_state.sqlite",
    )
    parser.add_argument("--source")
    parser.add_argument("--entity")
    parser.add_argument("--endpoint", default="identity_lookup")
    args = parser.parse_args()

    cache = EvidenceQueryCache(args.db.resolve())
    try:
        if args.source and args.entity:
            result = cache.get_state(
                source_id=args.source,
                entity_key=args.entity,
                endpoint=args.endpoint,
            )
            print(json.dumps(result or {
                "source_id": args.source,
                "entity_key": args.entity,
                "endpoint": args.endpoint,
                "status": "not_queried",
            }, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(cache.summary(), ensure_ascii=False, indent=2))
    finally:
        cache.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
