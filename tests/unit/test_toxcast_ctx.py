"""ToxCast/CTX importer + QC tests (fixture / mocked network)."""

from __future__ import annotations

import json
from pathlib import Path

from services.public_data.assay_index import row_to_evidence_hit
from services.public_data.qc import filter_records, qc_toxcast_row
from services.public_data.toxcast_ctx import (
    ASSAY_GRAIN_FIELDS,
    AuthMissingError,
    import_toxcast_ctx,
    normalize_toxcast_row,
)


SOURCE = {
    "source_id": "epa_toxcast_tox21",
    "api_base": "https://comptox.epa.gov/ctx-api/bioactivity",
    "license_policy": "preserve_version_and_endpoint_provenance",
}

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "data/public/fixtures/toxcast_ctx"


def test_normalize_active_hit_is_risk_signal() -> None:
    row = normalize_toxcast_row(
        {
            "dtxsid": "DTXSID7020182",
            "aeid": 701,
            "assayComponentEndpointName": "TOX21_ERa_LUC_BG1_Agonist",
            "hitc": 1.0,
            "ac50": 0.85,
            "casn": "80-05-7",
            "inchikey": "IISBACLAFKSPIT-UHFFFAOYSA-N",
            "smiles": "CC(C)(c1ccc(O)cc1)c1ccc(O)cc1",
        },
        source_id="epa_toxcast_tox21",
        license_policy="test",
        api_base=SOURCE["api_base"],
        retrieved_at="2026-07-15T00:00:00+00:00",
    )
    for field in ASSAY_GRAIN_FIELDS:
        assert field in row
    assert row["active_hit"] is True
    assert row["evidence_role"] == "risk_signal"
    assert row["direction"] == "risk"


def test_normalize_inactive_is_not_safety_label() -> None:
    row = normalize_toxcast_row(
        {
            "dtxsid": "DTXSID7020182",
            "aeid": 703,
            "hitc": 0.0,
            "ac50": None,
        },
        source_id="epa_toxcast_tox21",
        license_policy="test",
        api_base=SOURCE["api_base"],
    )
    assert row["active_hit"] is False
    assert row["evidence_role"] == "annotation_only"
    assert row["direction"] == "unknown"


def test_import_from_fixtures_without_api_key() -> None:
    result = import_toxcast_ctx(
        SOURCE,
        limit=20,
        per_dtxsid_limit=5,
        dtxsids=("DTXSID7020182", "DTXSID3020966"),
        api_key=None,
        fixture_dir=FIXTURES,
        allow_fixture_fallback=True,
        cache_dir=None,
    )
    assert result["activity_count"] >= 3
    assert result["active_hit_count"] >= 2
    assert result["ranking_effect"] == "risk_signal_only"
    assert result["mode_counts"].get("fixture", 0) >= 1
    assert result["api_key_present"] is False


def test_import_auth_missing_without_fixture_or_key(tmp_path: Path) -> None:
    try:
        import_toxcast_ctx(
            SOURCE,
            dtxsids=("DTXSID0000000",),
            api_key=None,
            fixture_dir=tmp_path / "empty",
            allow_fixture_fallback=True,
            cache_dir=tmp_path / "cache",
        )
        raised = False
    except AuthMissingError:
        raised = True
    assert raised is True


def test_toxcast_qc_keeps_active_risk_only() -> None:
    rows = [
        {
            "compound_id": "DTXSID7020182",
            "standardized_smiles": "CC(C)(c1ccc(O)cc1)c1ccc(O)cc1",
            "inchikey": "IISBACLAFKSPIT-UHFFFAOYSA-N",
            "assay_id": "ToxCast:aeid:701",
            "endpoint": "TOX21_ERa",
            "active_hit": True,
            "hitc": 1.0,
            "value": 0.85,
            "classification": "active_risk",
        },
        {
            "compound_id": "DTXSID7020182",
            "standardized_smiles": "CC(C)(c1ccc(O)cc1)c1ccc(O)cc1",
            "inchikey": "IISBACLAFKSPIT-UHFFFAOYSA-N",
            "assay_id": "ToxCast:aeid:703",
            "endpoint": "TOX21_PPARg",
            "active_hit": False,
            "hitc": 0.0,
            "classification": "inactive_or_inconclusive",
        },
    ]
    kept, reasons = filter_records(rows, source="epa_toxcast_tox21")
    assert len(kept) == 1
    assert reasons.get("inactive_excluded_not_safety_label") == 1
    assert kept[0]["evidence_role"] == "risk_signal"
    hit = row_to_evidence_hit(kept[0])
    assert hit.query_type == "tox"
    assert hit.direction == "risk"
    assert hit.score > 0
    ok, reason, _ = qc_toxcast_row(rows[1])
    assert ok is False
    assert "inactive" in reason
