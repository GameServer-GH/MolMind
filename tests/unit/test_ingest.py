"""services.ingest：sample.sdf 解析；坏分子计入 skipped；stderr 无 RDKit ERROR。"""

from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path

from services.ingest import (
    estimate_sdf_record_count,
    parse_sdf,
    parse_sdf_detailed,
    quiet_rdkit,
)

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SDF = ROOT / "data" / "sample.sdf"


def test_parse_sample_sdf_count() -> None:
    result = parse_sdf_detailed(SAMPLE_SDF)
    assert result.raw_count >= 10
    assert result.parsed_count >= 10
    assert result.parsed_count == len(result.records)
    assert all(r.molecule_id for r in result.records)
    assert all(r.smiles for r in result.records)


def test_estimate_sdf_record_count_matches_parse() -> None:
    estimated = estimate_sdf_record_count(SAMPLE_SDF)
    result = parse_sdf_detailed(SAMPLE_SDF)
    assert estimated == result.raw_count
    assert estimated >= 10


def test_parse_progress_callback_reports_monotonic_counts() -> None:
    snapshots: list[tuple[int, int, int]] = []

    def on_progress(snap) -> None:
        snapshots.append((snap.processed, snap.valid, snap.skipped))

    estimated = estimate_sdf_record_count(SAMPLE_SDF)
    result = parse_sdf_detailed(
        SAMPLE_SDF,
        on_progress=on_progress,
        estimated_total=estimated,
    )
    assert snapshots[0] == (0, 0, 0)
    assert snapshots[-1][0] == result.raw_count
    assert snapshots[-1][1] == result.parsed_count
    assert snapshots[-1][2] == result.skipped
    processed = [item[0] for item in snapshots]
    assert processed == sorted(processed)
    assert max(processed) == result.raw_count


def test_bad_molecule_counted_as_skipped(tmp_path: Path) -> None:
    # Minimal invalid SDF block: zero atoms but claims connectivity → sanitize fail / None
    bad = tmp_path / "bad.sdf"
    bad.write_text(
        "BADMOL\n"
        "     RDKit          2D\n"
        "\n"
        "  0  0  0  0  0  0  0  0  0  0999 V2000\n"
        "M  END\n"
        "$$$$\n"
        "GOOD\n"
        "     RDKit          2D\n"
        "\n"
        "  1  0  0  0  0  0  0  0  0  0999 V2000\n"
        "    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\n"
        "M  END\n"
        "$$$$\n",
        encoding="utf-8",
    )
    with quiet_rdkit():
        result = parse_sdf_detailed(bad)
    assert result.raw_count == 2
    assert result.skipped >= 1
    assert result.parsed_count >= 1
    assert result.issues
    assert all(issue.status == "invalid" and issue.reason_code for issue in result.issues)


def test_stderr_quiet_no_rdkit_error_spam() -> None:
    buf = StringIO()
    old = sys.stderr
    sys.stderr = buf
    try:
        with quiet_rdkit():
            parse_sdf(SAMPLE_SDF)
    finally:
        sys.stderr = old
    err = buf.getvalue()
    assert "ERROR:" not in err
    assert "Could not sanitize" not in err
