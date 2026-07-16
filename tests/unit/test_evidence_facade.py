"""services.evidence_facade：snapshot 优先、offline 空包、熔断。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from services.evidence_facade import EvidenceBundle, EvidenceFacade
from services.pipeline.config_loader import load_config

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


def test_snapshot_row_is_indexed_by_inchikey_and_cas(tmp_path: Path) -> None:
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
        "schema_version": "evidence-v2",
    }
    (tmp_path / "v2.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    cfg = load_config(mode="auto")
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path)
    with patch.object(facade, "_try_live") as live:
        bundle = facade.query(
            inchikey="STANDARDIZED-KEY-N",
            cas="123-45-6",
            smiles="CCO",
        )
    live.assert_not_called()
    assert bundle.lipid[0].evidence_id == "chembl:test"


def test_query_failure_is_not_cached_as_database_miss(tmp_path: Path) -> None:
    cfg = load_config(mode="online")
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path)
    with patch.object(facade, "_try_live", return_value=[]) as live:
        live.side_effect = lambda **_kwargs: (
            setattr(facade, "_live_failures", facade._live_failures + 1) or []
        )
        facade.query(
            inchikey="FAILED-KEY-N",
            cas="555-55-5",
            smiles="CCO",
        )
    assert not (tmp_path / "auto_cache.jsonl").exists()


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
