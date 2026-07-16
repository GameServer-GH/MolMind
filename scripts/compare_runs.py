"""Compare two MolMind nomination CSVs without changing ranking."""

from __future__ import annotations

import argparse
from pathlib import Path

from services.pipeline.run_diff import write_run_diff


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare two MolMind nomination CSVs")
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write_run_diff(args.baseline, args.candidate, args.output)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
