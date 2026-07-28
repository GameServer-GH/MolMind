"""Evidence adapters：JSONL 回放与 404/超时降级。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from services.evidence_facade import EvidenceFacade
from services.evidence_facade.facade import _classify_chembl_activity
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
            "endpoint": "cellular_lipid_reduction",
            "direction": "supports",
            "evidence_role": "task_evidence",
            "schema_version": "evidence-v2",
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


def test_chembl_404_is_verified_empty_not_a_scoring_signal(tmp_path: Path) -> None:
    cfg = load_config(mode="online")
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path)

    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.content = b'{}'

    with patch.object(httpx.Client, "get", return_value=mock_resp):
        hits = facade._try_live(
            inchikey="NOTFOUND-AAAA-BBBBBBBBBBB-N",
            cas=None,
            smiles="CCO",
        )

    assert len(hits) == 2  # ChEMBL and PubChem are independently verified.
    assert {hit.adapter_id for hit in hits} == {"chembl_lipid_v1", "pubchem_tox_v1"}
    assert all(hit.query_type == "query_audit" for hit in hits)
    assert all(hit.query_status == "verified_empty" for hit in hits)
    assert all(hit.score == 0.0 for hit in hits)


def test_timeout_degrades_without_crashing(tmp_path: Path) -> None:
    cfg = load_config(mode="online")
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path)

    with patch.object(httpx.Client, "get", side_effect=httpx.TimeoutException("timeout")):
        hits = facade._try_live(
            inchikey="TIMEOUT-AAAA-BBBBBBBBBBB-N",
            cas=None,
            smiles="CCO",
        )

    assert len(hits) == 1
    assert hits[0].query_type == "query_audit"
    assert hits[0].query_status == "timeout"
    assert hits[0].provenance_status == "query_failed"
    assert hits[0].score == 0.0
    assert facade._live_failures >= 1
    assert "evidence_live" in cfg.degraded_channels
    assert "evidence_live_timeout" in cfg.degraded_channels


def test_pubchem_multiple_cids_requires_identity_review_without_toxicity_lookup(
    tmp_path: Path,
) -> None:
    cfg = load_config(mode="online")
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path)
    cid_response = MagicMock()
    cid_response.status_code = 200
    cid_response.content = b'{"IdentifierList":{"CID":[22,11,22]}}'
    cid_response.json.return_value = {"IdentifierList": {"CID": [22, 11, 22]}}
    cid_response.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = cid_response

    hits = facade._pubchem_tox(client, "AMBIGUOUS-IDENTITY-KEY")

    assert client.get.call_count == 1
    assert len(hits) == 1
    assert hits[0].query_type == "query_audit"
    assert hits[0].query_status == "identity_review_required"
    assert hits[0].score == 0.0
    assert hits[0].payload["cids"] == [11, 22]


def test_pubchem_server_failure_is_query_failure_not_verified_empty(
    tmp_path: Path,
) -> None:
    facade = EvidenceFacade(load_config(mode="online"), snapshot_dir=tmp_path)
    request = httpx.Request("GET", "https://pubchem.example.test")
    cid = httpx.Response(
        200,
        json={"IdentifierList": {"CID": [11]}},
        request=request,
    )
    unavailable = httpx.Response(503, content=b"unavailable", request=request)
    client = MagicMock()
    client.get.side_effect = [cid, unavailable, unavailable]

    with pytest.raises(httpx.HTTPStatusError):
        facade._pubchem_tox(client, "SERVER-FAILURE-KEY")


def test_pubchem_cas_uses_name_namespace_and_records_lookup_basis(
    tmp_path: Path,
) -> None:
    facade = EvidenceFacade(load_config(mode="online"), snapshot_dir=tmp_path)
    request = httpx.Request("GET", "https://pubchem.example.test")
    not_found = httpx.Response(404, content=b"{}", request=request)
    client = MagicMock()
    client.get.return_value = not_found

    hits = facade._pubchem_tox(
        client,
        "64-17-5",
        lookup_field="cas",
        lookup_value="64-17-5",
    )

    requested_url = str(client.get.call_args.args[0])
    assert "/compound/name/64-17-5/cids/JSON" in requested_url
    assert hits[0].query_status == "verified_empty"
    assert hits[0].payload["lookup_field"] == "cas"


def test_live_adapters_use_configurable_api_bases(tmp_path: Path) -> None:
    facade = EvidenceFacade(load_config(mode="online"), snapshot_dir=tmp_path)
    not_found = MagicMock()
    not_found.status_code = 404
    not_found.content = b"{}"
    client = MagicMock()
    client.get.return_value = not_found

    facade._chembl_lipid(
        client,
        "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
        api_base="https://chembl.example.test/api",
    )
    assert str(client.get.call_args.args[0]).startswith(
        "https://chembl.example.test/api/"
    )

    client.reset_mock()
    client.get.return_value = not_found
    facade._pubchem_tox(
        client,
        "64-17-5",
        lookup_field="cas",
        lookup_value="64-17-5",
        api_base="https://pubchem.example.test/rest/pug",
    )
    assert str(client.get.call_args.args[0]).startswith(
        "https://pubchem.example.test/rest/pug/compound/name/"
    )


@pytest.mark.parametrize(
    ("activity", "expected"),
    [
        (
            {"assay_description": "Induction of phospholipidosis in HepG2 cells"},
            "adverse_phenotype",
        ),
        (
            {
                "assay_description": "Reduced neutral lipid accumulation in HepG2 cells",
                "bao_label": "cell-based format",
            },
            "positive_phenotype",
        ),
        (
            {
                "assay_description": (
                    "Reduction in lipid accumulation in human HepG2 cells at 1 microM "
                    "by Oil Red O staining"
                ),
            },
            "positive_phenotype",
        ),
        (
            {
                "assay_description": (
                    "Antioxidant activity against H2O2-induced lipid accumulation in "
                    "HUVEC cells assessed as reduction in MDA level"
                ),
            },
            "positive_phenotype",
        ),
        (
            {
                "assay_description": (
                    "Inhibition of LAL in intact human GM05659 cells assessed as "
                    "increase in neutral lipid accumulation"
                ),
            },
            "adverse_phenotype",
        ),
        (
            {
                "assay_description": (
                    "Antihyperlipidemic activity in HFD-induced hyperlipidemic rat "
                    "assessed as decrease in macrovesicular steatosis"
                ),
            },
            "annotation",
        ),
        (
            {
                "target_pref_name": "Peroxisome proliferator-activated receptor alpha",
                "standard_type": "IC50",
            },
            "mechanism",
        ),
        (
            {"assay_description": "Lipid binding assay with no reported direction"},
            "annotation",
        ),
    ],
)
def test_chembl_activity_direction_classifier(activity: dict, expected: str) -> None:
    assert _classify_chembl_activity(activity) == expected


def test_legacy_phospholipidosis_is_replayed_as_toxicity_not_lipid(tmp_path: Path) -> None:
    row = {
        "inchikey": "DGMKFQYCZXERLX-UHFFFAOYSA-N",
        "cas": "6620-60-6",
        "adapter_id": "chembl_lipid_v1",
        "query_type": "lipid",
        "score": 0.5,
        "confidence": 0.61,
        "evidence_id": "chembl:CHEMBL316561:lipid",
        "payload": {
            "chembl_id": "CHEMBL316561",
            "lipid_hits": 2,
            "targets": ["Unchecked", "Phospholipidosis"],
        },
    }
    (tmp_path / "legacy.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    facade = EvidenceFacade(load_config(mode="offline"), snapshot_dir=tmp_path)
    bundle = facade.query(
        inchikey=row["inchikey"],
        cas=row["cas"],
        smiles="CCO",
        allow_live=False,
    )
    assert bundle.lipid_score == 0.0
    assert bundle.conf_e == 0.0
    assert bundle.tox_score == pytest.approx(0.5)
    assert bundle.tox[0].direction == "risk"


def test_legacy_target_binding_is_mechanism_not_positive_lipid(tmp_path: Path) -> None:
    row = {
        "inchikey": "MECHANISM-KEY-N",
        "cas": "",
        "adapter_id": "chembl_lipid_v1",
        "query_type": "lipid",
        "score": 0.7,
        "confidence": 0.77,
        "evidence_id": "chembl:CHEMBL595:lipid",
        "payload": {"targets": ["Peroxisome proliferator-activated receptor gamma"]},
    }
    (tmp_path / "legacy.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    facade = EvidenceFacade(load_config(mode="offline"), snapshot_dir=tmp_path)
    bundle = facade.query(
        inchikey=row["inchikey"],
        cas=None,
        smiles="CCO",
        allow_live=False,
    )
    assert bundle.lipid_score == 0.0
    assert bundle.conf_e == 0.0
    assert bundle.pathway
    assert bundle.pathway[0].evidence_role == "mechanism_support"
