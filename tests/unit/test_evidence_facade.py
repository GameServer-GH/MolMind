"""services.evidence_facade：snapshot 优先、offline 空包、熔断。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from services.evidence_facade import EvidenceBundle, EvidenceFacade
from services.pipeline.config_loader import load_config
from plugins.molmind_core.scientific.evidence_facade.facade import (
    _normalize_snapshot_row,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def snapshot_jsonl(tmp_path: Path) -> Path:
    path = tmp_path / "fixture.jsonl"
    rows = [
        {
            "inchikey": "TESTKEY-AAAAA-BBBBBBBBBB-N",
            "cas": "123-45-6",
            "adapter_id": "chembl_lipid_v1",
            "query_type": "lipid",
            "score": 0.72,
            "confidence": 0.6,
            "evidence_id": "chembl:TEST:lipid",
            "payload": {},
            "endpoint": "cellular_lipid_reduction",
            "direction": "supports",
            "evidence_role": "task_evidence",
            "query_status": "exact_hit",
            "schema_version": "evidence-v2",
        },
        {
            "inchikey": "TESTKEY-AAAAA-BBBBBBBBBB-N",
            "cas": "123-45-6",
            "adapter_id": "pubchem_tox_v1",
            "query_type": "tox",
            "score": 0.35,
            "confidence": 0.55,
            "evidence_id": "pubchem:999:ghs",
            "payload": {},
            "query_status": "exact_hit",
        },
    ]
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


def test_snapshot_hit_skips_http(snapshot_jsonl: Path) -> None:
    cfg = load_config(mode="online")
    facade = EvidenceFacade(cfg, snapshot_dir=snapshot_jsonl.parent)

    with patch.object(httpx.Client, "get", MagicMock()) as mock_get:
        bundle = facade.query(
            inchikey="TESTKEY-AAAAA-BBBBBBBBBB-N",
            cas="123-45-6",
            smiles="CCO",
            allow_live=True,
        )

    mock_get.assert_not_called()
    assert bundle.lipid_score == pytest.approx(0.72)
    assert bundle.tox_score == pytest.approx(0.35)
    assert bundle.has_any


def test_offline_no_snapshot_returns_empty_no_forged_scores(tmp_path: Path) -> None:
    cfg = load_config(mode="offline")
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path / "empty")

    bundle = facade.query(
        inchikey="UNKNOWN-AAAAA-BBBBBBBBBB-N",
        cas=None,
        smiles="CCO",
        allow_live=False,
    )

    assert not bundle.has_any
    assert bundle.lipid_score == 0.0
    assert bundle.tox_score == 0.0
    assert bundle.conf_e == 0.0
    assert bundle.novelty_score == pytest.approx(0.0)


def test_unrelated_snapshot_does_not_hide_candidate_evidence_miss(snapshot_jsonl: Path) -> None:
    cfg = load_config(mode="offline")
    facade = EvidenceFacade(cfg, snapshot_dir=snapshot_jsonl.parent)
    bundle = facade.query(
        inchikey="UNRELATED-KEY-N",
        cas=None,
        smiles="CCO",
        allow_live=False,
    )
    assert not bundle.has_any
    assert facade._index  # 目录有数据，但本候选没有命中
    facade.finalize_degraded_flags(any_hit=False)
    assert "evidence_empty" in cfg.degraded_channels


def test_circuit_open_stops_live_attempts(tmp_path: Path) -> None:
    cfg = load_config(mode="online")
    cfg.raw["evidence"]["circuit_fail_threshold"] = 2
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path / "empty")

    def _boom(*_args, **_kwargs):
        raise httpx.TimeoutException("timeout")

    with patch.object(httpx.Client, "get", side_effect=_boom):
        facade._try_live(inchikey="MISSING-AAAAA-BBBBBBBBBB-N", cas=None, smiles="CCO")
        facade._try_live(inchikey="MISSING2-AAAA-BBBBBBBBBBB-N", cas=None, smiles="CCC")
        assert facade._circuit_open

        mock_get = MagicMock()
        with patch.object(httpx.Client, "get", mock_get):
            bundle = facade.query(
                inchikey="MISSING3-AAAA-BBBBBBBBBBB-N",
                cas=None,
                smiles="CCCC",
                allow_live=True,
            )

    mock_get.assert_not_called()
    assert not bundle.has_any


def test_direct_live_call_reports_circuit_open_as_not_queried(tmp_path: Path) -> None:
    cfg = load_config(mode="online")
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path)
    facade._circuit_open = True

    hits = facade._try_live(inchikey="CIRCUIT-OPEN-N", cas=None, smiles="CCO")

    assert len(hits) == 1
    assert hits[0].query_status == "not_queried"
    assert hits[0].provenance_status == "query_failed"
    assert hits[0].payload["reason"] == "circuit_open"


def test_bundle_all_ids(snapshot_jsonl: Path) -> None:
    cfg = load_config(mode="offline")
    facade = EvidenceFacade(cfg, snapshot_dir=snapshot_jsonl.parent)
    bundle = facade.query(
        inchikey="TESTKEY-AAAAA-BBBBBBBBBB-N",
        cas="123-45-6",
        smiles="CCO",
        allow_live=False,
    )
    ids = bundle.all_ids()
    assert "chembl:TEST:lipid" in ids
    assert "pubchem:999:ghs" in ids


def test_snapshot_row_can_use_cas_when_structure_key_is_missing(tmp_path: Path) -> None:
    row = {
        "inchikey": "EXACT-KEY-N",
        "cas": "123-45-6",
        "adapter_id": "chembl_lipid_v1",
        "query_type": "lipid",
        "score": 0.5,
        "confidence": 0.5,
        "evidence_id": "chembl:test",
        "endpoint": "cellular_lipid_reduction",
        "direction": "supports",
        "evidence_role": "task_evidence",
        "query_status": "exact_hit",
        "schema_version": "evidence-v2",
    }
    (tmp_path / "v2.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    cfg = load_config(mode="auto")
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path)
    with patch.object(facade, "_try_live") as live:
        bundle = facade.query(
            inchikey="",
            cas="123-45-6",
            smiles="CCO",
        )
    live.assert_not_called()
    assert bundle.lipid[0].evidence_id == "chembl:test"
    assert bundle.lipid[0].lookup_field == "cas"
    assert bundle.lipid[0].lookup_value == "123-45-6"


def test_snapshot_cas_identity_conflict_blocks_lifts_but_keeps_tox_risk(
    tmp_path: Path,
) -> None:
    conflicting_inchikey = "OTHER-STRUCTURE-KEY-N"
    cas = "50-00-0"
    common = {
        "inchikey": conflicting_inchikey,
        "cas": cas,
        "confidence": 0.8,
        "evidence_role": "task_evidence",
        "query_status": "exact_hit",
        "schema_version": "evidence-v2",
    }
    rows = [
        {
            **common,
            "adapter_id": "chembl_lipid_v1",
            "query_type": "lipid",
            "score": 0.9,
            "evidence_id": "chembl:conflict:lipid",
            "endpoint": "cellular_lipid_reduction",
            "direction": "supports",
        },
        {
            **common,
            "adapter_id": "chembl_lipid_v1",
            "query_type": "novelty",
            "score": 0.85,
            "evidence_id": "chembl:conflict:novelty",
            "endpoint": "novelty",
            "direction": "supports",
        },
        {
            **common,
            "adapter_id": "chembl_lipid_v1",
            "query_type": "tox",
            "score": 0.95,
            "evidence_id": "chembl:conflict:safety",
            "endpoint": "safety_clearance",
            "direction": "supports_safety",
        },
        {
            **common,
            "adapter_id": "pubchem_tox_v1",
            "query_type": "tox",
            "score": 0.7,
            "confidence": 0.55,
            "evidence_id": "pubchem:conflict:risk",
            "endpoint": "hazard_classification",
            "direction": "risk",
        },
    ]
    snapshot = tmp_path / "conflict.jsonl"
    snapshot.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    cfg = load_config(mode="offline")
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path)

    bundle = facade.query(
        inchikey="CANDIDATE-STRUCTURE-KEY-N",
        cas=cas,
        smiles="CCO",
        allow_live=False,
    )

    assert bundle.has_identity_review_required
    assert bundle.lipid_score == 0.0
    assert bundle.conf_e == 0.0
    assert bundle.novelty_score == 0.0
    assert not bundle.has_safety_clearance_evidence
    assert bundle.tox_score == pytest.approx(0.7)
    assert bundle.toxicity_evidence_coverage == pytest.approx(0.55)
    assert {hit.evidence_id for hit in bundle.tox} == {"pubchem:conflict:risk"}
    assert bundle.tox[0].match_type == "cas_identifier_conflict_conservative_risk"
    assert bundle.tox[0].claim_ceiling == (
        "candidate_risk_signal_only_not_safety_clearance"
    )
    reviews = [
        hit
        for hit in bundle.query_audit
        if hit.query_status == "identity_review_required"
    ]
    assert {hit.adapter_id for hit in reviews} == {
        "chembl_lipid_v1",
        "pubchem_tox_v1",
    }
    assert all(hit.score == 0.0 and hit.confidence == 0.0 for hit in reviews)
    assert all(hit.lookup_field == "cas" and hit.lookup_value == cas for hit in reviews)
    assert all(hit.match_type == "cas_identifier_conflict" for hit in reviews)
    assert all(
        hit.payload["reason"] == "cas_snapshot_inchikey_conflict"
        for hit in reviews
    )


def test_legacy_snapshot_status_migration_is_strict() -> None:
    base = {
        "query_type": "tox",
        "score": 0.8,
        "confidence": 0.7,
        "evidence_id": "legacy:test",
        "evidence_role": "task_evidence",
        "provenance_status": "retrieved",
        "direction": "risk",
    }

    known = _normalize_snapshot_row({**base, "adapter_id": "pubchem_tox_v1"})
    unknown = _normalize_snapshot_row({**base, "adapter_id": "unknown_adapter_v1"})

    assert known["query_status"] == "exact_hit"
    assert known["provenance_status"] == "legacy_snapshot_migrated"
    assert unknown["query_status"] == "not_queried"


def test_facade_never_uses_or_caches_legacy_live_query(tmp_path: Path) -> None:
    cfg = load_config(mode="online")
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path)
    with patch.object(facade, "_try_live", return_value=[]) as live:
        facade.query(
            inchikey="FAILED-KEY-N",
            cas="555-55-5",
            smiles="CCO",
            allow_live=True,
        )
    live.assert_not_called()
    assert not (tmp_path / "auto_cache.jsonl").exists()
    assert "legacy_facade_live_blocked" in cfg.degraded_channels


def test_in_memory_cache_key_includes_input_structure(tmp_path: Path) -> None:
    cfg = load_config(mode="offline")
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path)

    first = facade.query(
        inchikey="SAME-IDENTITY-KEY-N",
        cas=None,
        smiles="CCO",
        allow_live=False,
    )
    second = facade.query(
        inchikey="SAME-IDENTITY-KEY-N",
        cas=None,
        smiles="CCN",
        allow_live=False,
    )

    assert first is not second
    assert first.input_structure_hash != second.input_structure_hash


def test_toxicity_risk_propagates_to_frozen_standardized_alias(tmp_path: Path) -> None:
    row = {
        "inchikey": "HYAFETHFCAUJAY-UHFFFAOYSA-N",
        "cas": "111025-46-8",
        "adapter_id": "pubchem_tox_v1",
        "query_type": "tox",
        "score": 0.7,
        "confidence": 0.55,
        "evidence_id": "pubchem:4829:ghs",
        "direction": "risk",
        "query_status": "exact_hit",
    }
    (tmp_path / "risk.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    cfg = load_config(mode="offline")
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path)
    bundle = facade.query(
        inchikey="IYYGBZJXHJSLEV-UHFFFAOYSA-N",
        cas="112529-15-4",
        smiles="CCO",
        allow_live=False,
    )
    assert bundle.tox_score == pytest.approx(0.7)
    assert bundle.tox[0].payload["identity_resolution"] == "risk_tautomer_or_parent_alias"
