from __future__ import annotations

import json
from pathlib import Path

def test_import_manifests_are_fail_closed() -> None:
    root = Path(__file__).resolve().parents[2]
    manifests = list((root / "data/public/manifests").glob("*.json"))
    assert manifests
    for path in manifests:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") == "molmind-public-assay-qc-v1":
            assert payload["missing_semantics"] == "audit_missing"
            assert payload["negative_search_is_negative_label"] is False
            continue
        assert payload["missing_semantics"] == "audit_missing"
        assert payload["negative_search_is_negative_label"] is False
        if payload["status"] in {"network_error", "planned", "audit_missing"}:
            assert "processed_path" not in payload


def test_chembl_manifest_is_assay_grain_imported() -> None:
    path = Path(__file__).resolve().parents[2] / "data/public/manifests/chembl_bioactivity.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "imported"
    assert payload["grain"] == "compound_x_assay_x_activity"
    assert payload["row_count"] >= 1
    assert payload["raw_sha256"]
    assert payload["processed_sha256"]
    assert payload["query"]["assay_count"] >= 1


def test_pubchem_manifest_has_structure_identity_stats() -> None:
    path = Path(__file__).resolve().parents[2] / "data/public/manifests/pubchem_bioassay.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "imported"
    assert payload["row_count"] >= 50
    assert payload["grain"] == "compound_x_assay_x_activity"
    query = payload["query"]
    assert len(query.get("aids") or []) >= 50
    assert query["unique_compound_count"] >= 1
    assert query["structure_identity_record_resolved"] == payload["row_count"]
    assert query["outcome_counts"]["Unspecified"] >= 0


def test_bindingdb_manifest_is_mechanism_support_imported() -> None:
    path = Path(__file__).resolve().parents[2] / "data/public/manifests/bindingdb.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "imported"
    assert payload["grain"] == "compound_x_assay_x_activity"
    assert payload["row_count"] >= 20
    query = payload["query"]
    assert query.get("ranking_effect") == "mechanism_support_only"
    assert query.get("target_count", 0) >= 8
    assert (query.get("target_label_counts") or {}).get("HMGCR", 0) >= 1


def test_toxcast_manifest_is_ctx_risk_signal_imported() -> None:
    path = Path(__file__).resolve().parents[2] / "data/public/manifests/epa_toxcast_tox21.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "imported"
    assert payload["grain"] == "compound_x_assay_x_activity"
    assert payload["row_count"] >= 5
    query = payload["query"]
    assert query.get("ranking_effect") == "risk_signal_only"
    assert query.get("active_hit_count", 0) >= 1
    assert query.get("dataset_version", "").startswith("ToxCast via CTX")


def test_dilirank_import_has_expected_release_size() -> None:
    path = Path(__file__).resolve().parents[2] / "data/public/manifests/fda_dilirank_2.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "imported"
    assert payload["query"]["dataset_version"] == "DILIrank 2.0"
    assert payload["query"]["row_count"] >= 1300
