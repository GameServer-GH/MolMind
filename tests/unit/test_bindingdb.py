"""BindingDB Wave-1 importer + QC tests (mocked network)."""

from __future__ import annotations

import json
from pathlib import Path

from services.public_data.assay_index import row_to_evidence_hit
from services.public_data.bindingdb import (
    ASSAY_GRAIN_FIELDS,
    import_bindingdb_assay_grain,
    normalize_bindingdb_row,
    parse_affinity_nM,
)
from services.public_data.qc import filter_records, qc_bindingdb_row


SOURCE = {
    "source_id": "bindingdb",
    "api_base": "https://www.bindingdb.org/rest",
    "license_policy": "split_provenance_for_bindingdb_and_imported_sources",
}


def test_parse_affinity_nM() -> None:
    assert parse_affinity_nM("80") == (80.0, None)
    assert parse_affinity_nM("<10") == (10.0, "<")
    assert parse_affinity_nM("bad") == (None, None)


def test_normalize_bindingdb_row_is_mechanism_support() -> None:
    row = normalize_bindingdb_row(
        {
            "query": "Peroxisome proliferator-activated receptor alpha",
            "monomerid": "50132570",
            "smile": "CCO",
            "affinity_type": "Ki",
            "affinity": "80",
            "pmid": "12951090",
            "doi": "10.1016/s0960-894x(03)00702-9",
        },
        source_id="bindingdb",
        license_policy="cc",
        uniprot="Q07869",
        target_label="PPARA",
        api_base=SOURCE["api_base"],
        retrieved_at="2026-07-15T00:00:00+00:00",
    )
    for field in ASSAY_GRAIN_FIELDS:
        assert field in row
    assert row["compound_id"] == "BindingDB:50132570"
    assert row["classification"] == "mechanism"
    assert row["evidence_role"] == "mechanism_support"
    assert row["value"] == 80.0
    assert row["unit"] == "nM"
    assert row["uniprot"] == "Q07869"


def test_import_bindingdb_uses_cache_and_limits(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "Q07869.json").write_text(
        json.dumps(
            {
                "getLindsByUniprotsResponse": {
                    "affinities": [
                        {
                            "query": "PPARA",
                            "monomerid": "1",
                            "smile": "CCO",
                            "affinity_type": "Ki",
                            "affinity": "12",
                            "pmid": "1",
                        },
                        {
                            "query": "PPARA",
                            "monomerid": "2",
                            "smile": "CCC",
                            "affinity_type": "IC50",
                            "affinity": "30",
                            "pmid": "2",
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (cache / "P04035.json").write_text(
        json.dumps(
            {
                "getLindsByUniprotsResponse": {
                    "affinities": [
                        {
                            "query": "HMGCR",
                            "monomerid": "9",
                            "smile": "CCCC",
                            "affinity_type": "Ki",
                            "affinity": "5",
                            "pmid": "9",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    def boom(url, params, timeout):
        raise AssertionError("network should not be used when cache is present")

    result = import_bindingdb_assay_grain(
        SOURCE,
        limit=3,
        per_target_limit=5,
        uniprots=(("Q07869", "PPARA"), ("P04035", "HMGCR")),
        cache_dir=cache,
        get_json=boom,
        raw_dir=tmp_path / "raw",
    )
    assert result["activity_count"] == 3
    labels = {r["target_label"] for r in result["records"]}
    assert "PPARA" in labels and "HMGCR" in labels
    assert result["ranking_effect"] == "mechanism_support_only"
    assert result["grain"] == "compound_x_assay_x_activity"
    assert all(r["evidence_role"] == "mechanism_support" for r in result["records"])


def test_bindingdb_qc_is_mechanism_support_zero_score() -> None:
    ok, reason, row = qc_bindingdb_row(
        {
            "compound_id": "BindingDB:1",
            "standardized_smiles": "CCO",
            "assay_id": "BindingDB:Q07869:Ki",
            "endpoint": "Ki",
            "value": 12.0,
            "unit": "nM",
            "uniprot": "Q07869",
            "target_label": "PPARA",
            "source_id": "bindingdb",
        }
    )
    assert ok is True
    assert reason == "pass"
    assert row["evidence_role"] == "mechanism_support"
    assert row["eligible_for_endpoint_training"] is False
    hit = row_to_evidence_hit(row)
    assert hit.query_type == "pathway"
    assert hit.score == 0.0
    assert hit.confidence == 0.0

    kept, reasons = filter_records(
        [
            {
                "compound_id": "BindingDB:1",
                "standardized_smiles": "CCO",
                "assay_id": "BindingDB:Q07869:Ki",
                "endpoint": "Ki",
                "value": None,
                "uniprot": "Q07869",
                "target_label": "PPARA",
            }
        ],
        source="bindingdb",
    )
    assert kept == []
    assert reasons.get("affinity_missing") == 1
