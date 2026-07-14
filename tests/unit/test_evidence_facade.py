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
    assert bundle.novelty_score == pytest.approx(0.5)


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
