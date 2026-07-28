"""Explicit evidence bake uses the unified, auditable Gateway path."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from rdkit import Chem

from packages.chem_core import compute_descriptors, morgan_fp
from packages.models import EvidenceHit, MoleculeRecord
from plugins.molmind_core.scientific.evidence_facade.bake import (
    _hit_to_row,
    bake_evidence_for_records,
    load_frozen_top10_records,
)
from plugins.molmind_core.scientific.evidence_facade.facade import EvidenceFacade
from plugins.molmind_core.scientific.evidence_gateway.retriever import EvidenceRetriever
from plugins.molmind_core.scientific.pipeline.config_loader import load_config


Adapter = Callable[[Any], list[EvidenceHit]]


def _record(molecule_id: str, smiles: str) -> MoleculeRecord:
    mol = Chem.MolFromSmiles(smiles)
    assert mol is not None
    desc = compute_descriptors(smiles)
    assert desc is not None
    return MoleculeRecord(
        molecule_id=molecule_id,
        smiles=smiles,
        inchikey=Chem.MolToInchiKey(mol),
        cas=None,
        mw=float(desc["mw"]),
        logp=float(desc["logp"]),
        hbd=int(desc["hbd"]),
        hba=int(desc["hba"]),
        tpsa=float(desc["tpsa"]),
        rotatable_bonds=int(desc["rotatable_bonds"]),
        aromatic_rings=int(desc["aromatic_rings"]),
        fp_bits=morgan_fp(mol),
        original_smiles=smiles,
        standardization_steps=("test_identity",),
    )


def _provider_config(
    tmp_path: Path,
    *,
    concurrency: int = 2,
    circuit_fail_threshold: int = 3,
    retry_minutes: int = 60,
) -> Path:
    payload = {
        "schema_version": "test-evidence-providers-v2",
        "cache": {
            "ttl_days": {
                "hit": 90,
                "annotation_only": 30,
                "verified_empty": 14,
            },
            "retry_minutes": {
                "query_failed": retry_minutes,
                "auth_missing": retry_minutes,
            },
        },
        "providers": {
            "chembl": {
                "enabled": True,
                "live_supported": True,
                "identity_order": ["original_inchikey", "standardized_inchikey"],
                "query_types": {"lipid": {"endpoint": "molecule_activity"}},
                "concurrency": concurrency,
                "timeout_sec": 2,
                "rate_limit_per_sec": 0,
                "retry_attempts": 0,
                "circuit_fail_threshold": circuit_fail_threshold,
                "circuit_reset_sec": 60,
                "adapter_version": "chembl-test-v1",
            },
            "pubchem": {
                "enabled": True,
                "live_supported": True,
                "identity_order": ["original_inchikey", "standardized_inchikey", "cas"],
                "query_types": {"tox": {"endpoint": "compound_bundle"}},
                "concurrency": concurrency,
                "timeout_sec": 2,
                "rate_limit_per_sec": 0,
                "retry_attempts": 0,
                "circuit_fail_threshold": circuit_fail_threshold,
                "circuit_reset_sec": 60,
                "adapter_version": "pubchem-test-v1",
            },
            "must_not_be_default": {
                "enabled": True,
                "live_supported": True,
                "identity_order": ["original_inchikey"],
                "query_types": {"pathway": {"endpoint": "unexpected"}},
                "concurrency": 1,
                "timeout_sec": 2,
                "adapter_version": "unexpected-v1",
            },
        },
    }
    path = tmp_path / "providers.yaml"
    # YAML is a superset of JSON, which keeps this fixture dependency-free.
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _scientific_hit(provider: str, task: Any) -> EvidenceHit:
    query_type = str(task.query_type).split(",")[0]
    return EvidenceHit(
        adapter_id=f"{provider}_test_v1",
        provider_id=provider,
        query_type=query_type,
        score=0.7,
        confidence=0.6,
        evidence_id=f"{provider}:{task.molecule_id}:{query_type}",
        payload={"molecule_id": task.molecule_id},
        evidence_role="task_evidence",
        evidence_type="endpoint_evidence",
        query_status="hit",
    )


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_hit_to_row_has_complete_gateway_contract() -> None:
    hit = EvidenceHit(
        adapter_id="chembl_lipid_v1",
        provider_id="chembl",
        query_type="lipid",
        score=0.7,
        confidence=0.6,
        evidence_id="chembl:X:lipid",
        payload={"chembl_id": "CHEMBL1"},
        evidence_role="task_evidence",
        evidence_type="endpoint_evidence",
        query_status="hit",
        lookup_field="original_inchikey",
        lookup_value="KEY-N",
        match_type="exact_original_inchikey",
        source_version="chembl-test",
        accession="CHEMBL1",
        claim_ceiling="candidate_activity_only",
    )
    row = _hit_to_row(hit, inchikey="KEY-N", cas="1-2-3")
    required = {
        "inchikey",
        "cas",
        "adapter_id",
        "provider_id",
        "query_type",
        "evidence_role",
        "evidence_type",
        "query_status",
        "lookup_field",
        "lookup_value",
        "match_type",
        "score",
        "confidence",
        "evidence_id",
        "endpoint",
        "direction",
        "source_url",
        "accession",
        "retrieved_at",
        "source_version",
        "adapter_version",
        "response_sha256",
        "claim_ceiling",
        "baked_at",
        "schema_version",
    }
    assert required <= set(row)


def test_hit_to_row_forces_query_audit_score_and_confidence_to_zero() -> None:
    audit = EvidenceHit(
        adapter_id="bad_transport_adapter",
        provider_id="chembl",
        query_type="query_audit",
        score=0.9,
        confidence=0.8,
        evidence_id="transport:must-not-score",
        evidence_role="query_audit",
        evidence_type="query_audit",
        query_status="query_failed",
    )
    row = _hit_to_row(audit, inchikey="KEY-N", cas=None)
    assert row["score"] == 0.0
    assert row["confidence"] == 0.0
    assert row["evidence_role"] == "query_audit"
    assert row["evidence_type"] == "query_audit"
    assert row["provenance_status"] == "query_failed"


def test_bake_batches_all_records_once_and_never_calls_try_live(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    records = [_record("BAKE1", "CCO"), _record("BAKE2", "CCN")]
    cfg = load_config(mode="online")
    out = tmp_path / "baked.jsonl"
    provider_config = _provider_config(tmp_path)
    calls: list[list[str]] = []
    original_query = EvidenceRetriever.query

    def query_spy(self: EvidenceRetriever, identities: Any, **kwargs: Any):
        materialized = list(identities)
        calls.append([str(item["molecule_id"]) for item in materialized])
        return original_query(self, materialized, **kwargs)

    monkeypatch.setattr(EvidenceRetriever, "query", query_spy)
    adapters = {
        "chembl": lambda task: [_scientific_hit("chembl", task)],
        "pubchem": lambda task: [_scientific_hit("pubchem", task)],
        "must_not_be_default": lambda _task: (_ for _ in ()).throw(
            AssertionError("non-default provider was queried")
        ),
    }
    with patch.object(
        EvidenceFacade,
        "_try_live",
        side_effect=AssertionError("legacy per-molecule live path was used"),
    ):
        stats = bake_evidence_for_records(
            records,
            cfg,
            output_path=out,
            skip_cached=False,
            provider_config_path=provider_config,
            cache_path=tmp_path / "state.sqlite",
            provider_adapters=adapters,
        )

    assert calls == [["BAKE1", "BAKE2"]]
    assert stats.fetched == 2
    assert stats.failures == 0
    assert stats.snapshot_sha256
    assert Path(stats.manifest_path).is_file()
    manifest = json.loads(Path(stats.manifest_path).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "molmind-evidence-bake-manifest-v3"
    assert manifest["query_entities"][0]["molecule_id"] == "BAKE1"
    assert {row["provider_id"] for row in _rows(out)} == {"chembl", "pubchem"}


def test_bake_query_audits_are_frozen_but_never_scored(tmp_path: Path) -> None:
    record = _record("EMPTY", "CCC")
    out = tmp_path / "empty.jsonl"
    stats = bake_evidence_for_records(
        [record],
        load_config(mode="online"),
        output_path=out,
        skip_cached=False,
        provider_config_path=_provider_config(tmp_path),
        cache_path=tmp_path / "state.sqlite",
        provider_adapters={"chembl": lambda _task: [], "pubchem": lambda _task: []},
    )

    rows = _rows(out)
    assert stats.failures == 0
    assert {row["query_status"] for row in rows} == {"verified_empty"}
    assert all(row["query_type"] == "query_audit" for row in rows)
    assert all(row["evidence_role"] == "query_audit" for row in rows)
    assert all(row["evidence_type"] == "query_audit" for row in rows)
    assert all(row["score"] == 0.0 and row["confidence"] == 0.0 for row in rows)
    assert all("biological negative" in row["payload"]["reason"] for row in rows)


def test_bake_provider_failure_is_isolated_and_output_order_is_stable(
    tmp_path: Path,
) -> None:
    records = [
        _record("ORDER1", "CCO"),
        _record("ORDER2", "CCN"),
        _record("ORDER3", "CCC"),
        _record("ORDER4", "CCCl"),
    ]
    provider_config = _provider_config(tmp_path, concurrency=2)
    lock = threading.Lock()
    active = {"chembl": 0, "pubchem": 0}
    max_active = {"chembl": 0, "pubchem": 0}
    calls = {"chembl": [], "pubchem": []}

    def adapter(provider: str) -> Adapter:
        def execute(task: Any) -> list[EvidenceHit]:
            with lock:
                calls[provider].append(task.molecule_id)
                active[provider] += 1
                max_active[provider] = max(max_active[provider], active[provider])
            try:
                # Later records finish first; serialized rows must still follow
                # input/task order rather than Future completion order.
                delay = 0.01 * (5 - int(str(task.molecule_id)[-1]))
                time.sleep(delay)
                if provider == "chembl" and task.molecule_id == "ORDER1":
                    raise RuntimeError("chembl test failure")
                return [_scientific_hit(provider, task)]
            finally:
                with lock:
                    active[provider] -= 1

        return execute

    out = tmp_path / "ordered.jsonl"
    stats = bake_evidence_for_records(
        records,
        load_config(mode="online"),
        output_path=out,
        skip_cached=True,
        provider_config_path=provider_config,
        cache_path=tmp_path / "state.sqlite",
        provider_adapters={"chembl": adapter("chembl"), "pubchem": adapter("pubchem")},
    )

    rows = _rows(out)
    assert stats.failures == 1
    assert set(calls["pubchem"]) == {record.molecule_id for record in records}
    assert set(calls["chembl"]) == {record.molecule_id for record in records}
    assert max_active == {"chembl": 2, "pubchem": 2}
    assert all(value <= 2 for value in max_active.values())
    first_row_by_molecule = list(dict.fromkeys(row["molecule_id"] for row in rows))
    assert first_row_by_molecule == [record.molecule_id for record in records]
    order1 = [row for row in rows if row["molecule_id"] == "ORDER1"]
    assert any(row["provider_id"] == "pubchem" and row["query_status"] == "hit" for row in order1)
    failed = next(
        row
        for row in order1
        if row["provider_id"] == "chembl" and row["query_status"] == "query_failed"
    )
    assert failed["provenance_status"] == "query_failed"
    assert failed["evidence_role"] == "query_audit"
    assert failed["score"] == 0.0


def test_bake_partial_snapshot_coverage_queries_only_missing_provider(
    tmp_path: Path,
) -> None:
    record = _record("PARTIAL", "CCO")
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()
    snapshot_row = {
        "molecule_id": record.molecule_id,
        "inchikey": record.inchikey,
        "adapter_id": "chembl_lipid_v3",
        "provider_id": "chembl",
        "query_type": "lipid",
        "score": 0.7,
        "confidence": 0.6,
        "evidence_id": "chembl:PARTIAL:lipid",
        "endpoint": "molecule_activity",
        "direction": "supports",
        "evidence_role": "task_evidence",
        "evidence_type": "endpoint_evidence",
        "query_status": "hit",
        "provenance_status": "retrieved",
        "lookup_field": "original_inchikey",
        "lookup_value": record.inchikey,
        "match_type": "exact_original_inchikey",
        "retrieved_at": "2026-07-27T00:00:00+00:00",
        "source_version": "chembl-test-v1",
        "adapter_version": "chembl-test-v1",
        "response_sha256": "frozen-response",
        "claim_ceiling": "candidate_preclinical_evidence_only",
    }
    (snapshot_dir / "partial.jsonl").write_text(
        json.dumps(snapshot_row) + "\n",
        encoding="utf-8",
    )
    calls = {"chembl": 0, "pubchem": 0}

    def unexpected_chembl(_task: Any) -> list[EvidenceHit]:
        calls["chembl"] += 1
        raise AssertionError("frozen ChEMBL coverage should suppress duplicate lookup")

    def pubchem(task: Any) -> list[EvidenceHit]:
        calls["pubchem"] += 1
        return [_scientific_hit("pubchem", task)]

    out = tmp_path / "partial-bake.jsonl"
    stats = bake_evidence_for_records(
        [record],
        load_config(mode="online"),
        output_path=out,
        skip_cached=True,
        provider_config_path=_provider_config(tmp_path),
        cache_path=tmp_path / "partial-state.sqlite",
        provider_adapters={"chembl": unexpected_chembl, "pubchem": pubchem},
        snapshot_dir=snapshot_dir,
    )

    assert calls == {"chembl": 0, "pubchem": 1}
    assert stats.skipped_cached == 0
    assert stats.fetched == 1
    assert {row["provider_id"] for row in _rows(out)} == {"pubchem"}


def test_bake_reuses_fresh_gateway_cache_without_adapter_calls(tmp_path: Path) -> None:
    record = _record("CACHE", "CCBr")
    provider_config = _provider_config(tmp_path)
    cache_path = tmp_path / "state.sqlite"
    adapters = {
        "chembl": lambda task: [_scientific_hit("chembl", task)],
        "pubchem": lambda task: [_scientific_hit("pubchem", task)],
    }
    first = bake_evidence_for_records(
        [record],
        load_config(mode="online"),
        output_path=tmp_path / "first.jsonl",
        skip_cached=True,
        provider_config_path=provider_config,
        cache_path=cache_path,
        provider_adapters=adapters,
    )
    assert first.fetched == 1

    def unexpected(_task: Any) -> list[EvidenceHit]:
        raise AssertionError("fresh Gateway cache should suppress live adapter")

    second_out = tmp_path / "second.jsonl"
    second = bake_evidence_for_records(
        [record],
        load_config(mode="online"),
        output_path=second_out,
        skip_cached=True,
        provider_config_path=provider_config,
        cache_path=cache_path,
        provider_adapters={"chembl": unexpected, "pubchem": unexpected},
    )
    assert second.fetched == 0
    assert second.failures == 0
    assert [row["evidence_id"] for row in _rows(second_out) if row["query_type"] != "query_audit"] == [
        "chembl:CACHE:lipid",
        "pubchem:CACHE:tox",
    ]


def test_bake_failure_backoff_is_provider_local(tmp_path: Path) -> None:
    record = _record("BACKOFF", "CCF")
    provider_config = _provider_config(tmp_path, retry_minutes=60)
    cache_path = tmp_path / "state.sqlite"
    pubchem_calls = 0

    def chembl_failure(_task: Any) -> list[EvidenceHit]:
        raise RuntimeError("temporary chembl failure")

    def pubchem_success(task: Any) -> list[EvidenceHit]:
        nonlocal pubchem_calls
        pubchem_calls += 1
        return [_scientific_hit("pubchem", task)]

    first_out = tmp_path / "backoff-first.jsonl"
    first = bake_evidence_for_records(
        [record],
        load_config(mode="online"),
        output_path=first_out,
        skip_cached=True,
        provider_config_path=provider_config,
        cache_path=cache_path,
        provider_adapters={"chembl": chembl_failure, "pubchem": pubchem_success},
    )
    assert first.failures == 1
    assert pubchem_calls == 1

    def should_not_retry(_task: Any) -> list[EvidenceHit]:
        raise AssertionError("query_failed backoff was bypassed")

    second_out = tmp_path / "backoff-second.jsonl"
    second = bake_evidence_for_records(
        [record],
        load_config(mode="online"),
        output_path=second_out,
        skip_cached=True,
        provider_config_path=provider_config,
        cache_path=cache_path,
        provider_adapters={"chembl": should_not_retry, "pubchem": should_not_retry},
    )
    assert second.fetched == 0
    assert second.failures == 1
    rows = _rows(second_out)
    assert any(row["provider_id"] == "pubchem" and row["query_status"] == "hit" for row in rows)
    chembl_audit = next(row for row in rows if row["provider_id"] == "chembl")
    assert chembl_audit["query_status"] == "query_failed"
    assert chembl_audit["score"] == 0.0
    assert chembl_audit["payload"]["reason"] == "retry backoff active"


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
