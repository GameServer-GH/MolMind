from __future__ import annotations

from pathlib import Path


def test_bulk_summary_script_exists() -> None:
    root = Path(__file__).resolve().parents[2]
    assert (root / "scripts/import_epa_ctx_bulk_summary.py").is_file()
