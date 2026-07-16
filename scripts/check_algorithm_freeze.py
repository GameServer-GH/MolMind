"""Fail when frozen scoring, eligibility or ranking semantics drift."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FREEZE_PATH = ROOT / "configs" / "algorithm_freeze.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_algorithm_freeze() -> list[str]:
    payload = json.loads(FREEZE_PATH.read_text(encoding="utf-8"))
    failures: list[str] = []
    for relative, expected in payload["critical_files"].items():
        path = ROOT / relative
        if not path.is_file():
            failures.append(f"missing: {relative}")
            continue
        actual = sha256_file(path)
        if actual != expected:
            failures.append(f"drift: {relative} expected={expected} actual={actual}")
    return failures


def main() -> int:
    failures = verify_algorithm_freeze()
    if failures:
        print("algorithm freeze FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("algorithm freeze OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
