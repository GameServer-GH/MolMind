"""bake-evidence：写出 JSONL schema。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from packages.models import EvidenceHit
from services.evidence_facade.bake import (
    _hit_to_row,
    bake_evidence_for_records,
    load_frozen_top10_records,
)
from services.pipeline.config_loader import load_config


def test_hit_to_row_has_required_fields() -> None:
    hit = EvidenceHit(
        adapter_id="chembl_lipid_v1",
        query_type="lipid",
        score=0.7,
        confidence=0.6,
        evidence_id="chembl:X:lipid",
        payload={"chembl_id": "CHEMBL1"},
    )
    row = _hit_to_row(hit, inchikey="KEY-N", cas="1-2-3")
    required = {
        "inchikey",
        "cas",
        "adapter_id",
        "query_type",
        "score",
        "confidence",
        "evidence_id",
        "payload",
        "baked_at",
        "schema_version",
        "source_url",
        "response_sha256",
        "provenance_status",
        "query_status",
    }
    assert required <= set(row.keys())


def test_bake_writes_jsonl_schema(tmp_path: Path) -> None:
    from packages.chem_core import compute_descriptors, morgan_fp
    from packages.models import MoleculeRecord
    from rdkit import Chem

    smiles = "CCO"
    mol = Chem.MolFromSmiles(smiles)
    assert mol is not None
    desc = compute_descriptors(smiles)
    assert desc is not None
    record = MoleculeRecord(
        molecule_id="BAKE1",
        smiles=smiles,
        inchikey=Chem.MolToInchiKey(mol) or "BAKE-KEY-N",
        cas=None,
        mw=float(desc["mw"]),
        logp=float(desc["logp"]),
        hbd=int(desc["hbd"]),
        hba=int(desc["hba"]),
        tpsa=float(desc["tpsa"]),
        rotatable_bonds=int(desc["rotatable_bonds"]),
        aromatic_rings=int(desc["aromatic_rings"]),
        fp_bits=morgan_fp(mol),
    )

    cfg = load_config(mode="online")
    out = tmp_path / "baked.jsonl"

    fake_hits = [
        EvidenceHit(
            adapter_id="chembl_lipid_v1",
            query_type="lipid",
            score=0.5,
            confidence=0.5,
            evidence_id="chembl:BAKE:lipid",
        )
    ]

    with patch(
        "services.evidence_facade.bake.EvidenceFacade._try_live",
        return_value=fake_hits,
    ):
        stats = bake_evidence_for_records([record], cfg, output_path=out, skip_cached=False)

    assert stats.wrote_rows >= 1
    assert stats.snapshot_sha256
    assert Path(stats.manifest_path).is_file()
    manifest = json.loads(Path(stats.manifest_path).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "molmind-evidence-bake-manifest-v3"
    assert len(manifest["candidate_set_sha256"]) == 64
    assert manifest["query_entities"][0]["molecule_id"] == "BAKE1"
    assert manifest["query_entities"][0]["standardized_inchikey"]
    assert out.is_file()
    line = out.read_text(encoding="utf-8").strip().splitlines()[0]
    row = json.loads(line)
    for key in ("inchikey", "adapter_id", "query_type", "score", "confidence", "evidence_id"):
        assert key in row


def test_bake_miss_is_query_audit_not_positive_evidence(tmp_path: Path) -> None:
    record = load_frozen_top10_records()[0]
    cfg = load_config(mode="online")
    out = tmp_path / "baked.jsonl"
    with patch("services.evidence_facade.bake.EvidenceFacade._try_live", return_value=[]):
        bake_evidence_for_records([record], cfg, output_path=out, skip_cached=False)
    row = json.loads(out.read_text(encoding="utf-8").strip())
    assert row["query_type"] == "query_audit"
    assert row["evidence_role"] == "query_audit"
    assert row["score"] == 0.0
    assert row["confidence"] == 0.0


def test_bake_preserves_typed_query_failure_for_retry(tmp_path: Path) -> None:
    record = load_frozen_top10_records()[0]
    cfg = load_config(mode="online")
    out = tmp_path / "baked.jsonl"

    def _timeout(self, **_kwargs):
        self._record_live_failure()
        return [
            EvidenceHit(
                adapter_id="evidence_live_v1",
                query_type="query_audit",
                score=0.0,
                confidence=0.0,
                evidence_id="timeout:test",
                evidence_role="query_audit",
                provenance_status="query_failed",
                query_status="timeout",
            )
        ]

    with patch("services.evidence_facade.bake.EvidenceFacade._try_live", _timeout):
        stats = bake_evidence_for_records([record], cfg, output_path=out, skip_cached=False)
    row = json.loads(out.read_text(encoding="utf-8").strip())
    assert stats.failures == 1
    assert row["query_status"] == "timeout"
    assert row["provenance_status"] == "query_failed"
    assert row["score"] == 0.0


def test_bake_network_policy_mutates_raw_evidence() -> None:
    from services.evidence_facade.bake import _apply_bake_network_policy

    cfg = load_config(mode="online")
    assert float(cfg.evidence["http_timeout_sec"]) == 4.0
    previous = _apply_bake_network_policy(cfg, candidate_count=10)
    assert float(cfg.evidence["http_timeout_sec"]) == 60.0
    assert int(cfg.evidence["circuit_fail_threshold"]) == 50
    evidence = cfg.raw["evidence"]
    for key, value in previous.items():
        evidence[str(key)] = value
    assert float(cfg.evidence["http_timeout_sec"]) == 4.0


def test_bake_continues_after_candidate_failure(tmp_path: Path) -> None:
    """Bake must not leave later shortlist members as sticky circuit not_queried."""
    records = load_frozen_top10_records()[:2]
    cfg = load_config(mode="online")
    out = tmp_path / "baked.jsonl"
    calls: list[str] = []

    def _fail_each(self, **kwargs):
        calls.append(kwargs["inchikey"])
        self._record_live_failure()
        return [
            EvidenceHit(
                adapter_id="evidence_live_v1",
                query_type="query_audit",
                score=0.0,
                confidence=0.0,
                evidence_id=f"error:{kwargs['inchikey']}",
                evidence_role="query_audit",
                provenance_status="query_failed",
                query_status="adapter_error",
            )
        ]

    with patch("services.evidence_facade.bake.EvidenceFacade._try_live", _fail_each):
        stats = bake_evidence_for_records(records, cfg, output_path=out, skip_cached=False)
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert stats.failures == 2
    assert len(calls) == 2
    assert [row["query_status"] for row in rows] == ["adapter_error", "adapter_error"]
    assert all(row["query_status"] != "verified_empty" for row in rows)


def test_bake_circuit_open_is_not_mislabeled_verified_empty(tmp_path: Path) -> None:
    records = load_frozen_top10_records()[:1]
    cfg = load_config(mode="online")
    out = tmp_path / "baked.jsonl"

    def _circuit_open(self, **kwargs):
        self._circuit_open = True
        return [
            EvidenceHit(
                adapter_id="evidence_live_v1",
                query_type="query_audit",
                score=0.0,
                confidence=0.0,
                evidence_id=f"circuit:{kwargs['inchikey']}",
                evidence_role="query_audit",
                provenance_status="query_failed",
                query_status="not_queried",
            )
        ]

    with patch("services.evidence_facade.bake.EvidenceFacade._try_live", _circuit_open):
        stats = bake_evidence_for_records(records, cfg, output_path=out, skip_cached=False)
    row = json.loads(out.read_text(encoding="utf-8").strip())
    assert stats.failures == 1
    assert row["query_status"] == "not_queried"
    assert row["query_status"] != "verified_empty"
    assert row["provenance_status"] == "query_failed"


def test_frozen_top10_records_preserve_original_query_identity() -> None:
    records = load_frozen_top10_records()
    assert len(records) == 10
    assert [record.molecule_id for record in records] == [
        "T37177",
        "T23557",
        "T64737",
        "T67958",
        "T84225",
        "T17044",
        "TN7120",
        "T39740",
        "T8188",
        "TN1037",
    ]
    t64737 = next(record for record in records if record.molecule_id == "T64737")
    assert t64737.inchikey == "JCZLABDVDPYLRZ-AWEZNQCLSA-N"
