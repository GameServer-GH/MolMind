"""Evidence adapters：JSONL 回放与 404/超时降级。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from services.evidence_facade import EvidenceFacade
from services.pipeline.config_loader import load_config


def _write_fixture(path: Path) -> None:
    rows = [
        {
            "inchikey": "REPLAY-AAAAA-BBBBBBBBBB-N",
            "cas": "",
            "adapter_id": "chembl_lipid_v1",
            "query_type": "lipid",
            "score": 0.65,
            "confidence": 0.7,
            "evidence_id": "chembl:REPLAY:lipid",
            "payload": {"chembl_id": "CHEMBL1"},
        },
        {
            "inchikey": "REPLAY-AAAAA-BBBBBBBBBB-N",
            "cas": "",
            "adapter_id": "pubchem_tox_v1",
            "query_type": "tox",
            "score": 0.42,
            "confidence": 0.55,
            "evidence_id": "pubchem:123:ghs",
            "payload": {"cid": 123},
        },
    ]
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_replay_fixture_writes_lipid_tox_scores(tmp_path: Path) -> None:
    snap_dir = tmp_path / "snap"
    snap_dir.mkdir()
    _write_fixture(snap_dir / "replay.jsonl")

    cfg = load_config(mode="offline")
    facade = EvidenceFacade(cfg, snapshot_dir=snap_dir)
    bundle = facade.query(
        inchikey="REPLAY-AAAAA-BBBBBBBBBB-N",
        cas=None,
        smiles="CCO",
        allow_live=False,
    )

    assert bundle.lipid_score == pytest.approx(0.65)
    assert bundle.tox_score == pytest.approx(0.42)
    assert any("chembl:REPLAY:lipid" == eid for eid in bundle.all_ids())


def test_chembl_404_returns_empty_gracefully(tmp_path: Path) -> None:
    cfg = load_config(mode="online")
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path)

    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch.object(httpx.Client, "get", return_value=mock_resp):
        hits = facade._try_live(
            inchikey="NOTFOUND-AAAA-BBBBBBBBBBB-N",
            cas=None,
            smiles="CCO",
        )

    assert hits == []


def test_timeout_degrades_without_crashing(tmp_path: Path) -> None:
    cfg = load_config(mode="online")
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path)

    with patch.object(httpx.Client, "get", side_effect=httpx.TimeoutException("timeout")):
        hits = facade._try_live(
            inchikey="TIMEOUT-AAAA-BBBBBBBBBBB-N",
            cas=None,
            smiles="CCO",
        )

    assert hits == []
    assert facade._live_failures >= 1
    assert "evidence_live" in cfg.degraded_channels
