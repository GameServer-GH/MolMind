"""Canonical query_evidence Tool: execution, offline safety and rank isolation."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from agent.registry import AgentRegistry
from packages.models import EvidenceHit, MoleculeRecord, ScoreRecord
from plugins.molmind_core.scientific.pipeline.run_identity import selection_sha256
from plugins.molmind_core.tools.scientific import (
    CORE_TOOL_HANDLERS,
    run_query_evidence,
)


INCHIKEY = "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
MOLECULE_ID = "T001"


@pytest.fixture
def provider_config(tmp_path):
    path = tmp_path / "evidence_providers.yaml"
    path.write_text(
        """
schema_version: molmind-evidence-providers-test-v1
cache:
  state_db: ignored-by-test.sqlite
  ttl_days:
    hit: 90
    verified_empty: 14
  retry_minutes:
    query_failed: 60
    auth_missing: 10
providers:
  chembl:
    enabled: true
    query_tool_default: true
    identity_order: [original_inchikey, standardized_inchikey, cas, standardized_smiles]
    endpoint: candidate_activity
    query_type: lipid
    adapter_version: chembl-test-v1
    concurrency: 1
    timeout_sec: 0.1
    retry_attempts: 0
""".lstrip(),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def snapshot_dir(tmp_path):
    path = tmp_path / "snapshot"
    path.mkdir()
    row = {
        "inchikey": INCHIKEY,
        "cas": "64-17-5",
        "adapter_id": "chembl_lipid_v1",
        "query_type": "lipid",
        "score": 0.72,
        "confidence": 0.61,
        "evidence_id": "chembl:TEST:lipid",
        "payload": {"chembl_id": "CHEMBL_TEST"},
        "endpoint": "cellular_lipid_reduction",
        "direction": "supports",
        "evidence_role": "task_evidence",
        # Prove that the public Tool normalizes the legacy adapter vocabulary.
        "query_status": "exact_hit",
        "retrieved_at": "2099-01-01T00:00:00+00:00",
        "adapter_version": "chembl-test-v1",
        "source_version": "chembl-test-source-v1",
        "response_sha256": "f" * 64,
        "schema_version": "evidence-v2",
    }
    (path / "fixture.jsonl").write_text(
        json.dumps(row, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _record() -> MoleculeRecord:
    return MoleculeRecord(
        molecule_id=MOLECULE_ID,
        smiles="CCO",
        inchikey=INCHIKEY,
        cas="64-17-5",
        mw=46.07,
        logp=-0.3,
        hbd=1,
        hba=1,
        tpsa=20.23,
        rotatable_bonds=0,
        aromatic_rings=0,
        original_smiles="OCC",
    )


def _score() -> ScoreRecord:
    return ScoreRecord(
        molecule_id=MOLECULE_ID,
        smiles="CCO",
        inchikey=INCHIKEY,
        cas="64-17-5",
        scaffold_smiles="",
        lipid_score=0.41,
        tox_risk=0.22,
        novelty_score=0.73,
        conf_e=0.18,
        final_score=0.53,
        tox_heads={"structure": 0.22},
        lipid_parts={"rule": 0.41},
        attributions=[],
        lipid_rationale="frozen lipid rationale",
        tox_rationale="frozen tox rationale",
        overall_reason="frozen ranking",
        eligibility_status="eligible",
        gated_out=False,
        selection_score=0.64,
        competition_scoring_version="test-v1",
        nomination_tier="primary",
    )


def _run_result(score: ScoreRecord | None = None) -> Any:
    selected = score or _score()
    digest = selection_sha256([selected])
    return SimpleNamespace(
        molecule_records=[_record()],
        scored_molecules=[selected],
        top_molecules=[selected],
        reserve_molecules=[],
        selection_sha256=digest,
    )


def _forbidden_adapter(calls: list[Any]):
    def adapter(task):
        calls.append(task)
        raise AssertionError("remote provider must not be called")

    return adapter


def _query_paths(tmp_path, snapshot_dir, provider_config) -> dict[str, Any]:
    return {
        "snapshot_dir": snapshot_dir,
        "provider_config_path": provider_config,
        "cache_path": tmp_path / "query-state.sqlite",
    }


def test_query_evidence_is_registered_with_real_read_only_handler() -> None:
    registry = AgentRegistry()

    assert CORE_TOOL_HANDLERS["query_evidence"] is run_query_evidence
    assert "query_evidence" in registry.tools
    assert registry.tools["query_evidence"].writes_selection is False
    assert registry.tools["query_evidence"].limits["allow_live_default"] is False
    assert registry.tools["query_evidence"].limits["writes_selection"] is False


def test_current_run_molecule_id_executes_and_preserves_frozen_selection(
    tmp_path, snapshot_dir, provider_config
) -> None:
    score = _score()
    result = _run_result(score)
    scientific_before = (
        score.lipid_score,
        score.tox_risk,
        score.novelty_score,
        score.conf_e,
    )
    selection_before = result.selection_sha256

    response = run_query_evidence(
        result=result,
        molecule_id=MOLECULE_ID,
        providers=["chembl"],
        query_types=["lipid"],
        allow_live=False,
        **_query_paths(tmp_path, snapshot_dir, provider_config),
    )

    assert response.ok is True
    assert response.error_code == ""
    assert response.identity["molecule_id"] == MOLECULE_ID
    # The Run retains original_smiles, so the resolver can derive and prefer
    # the original identity before the standardized key.
    assert response.identity["lookup_field"] == "original_inchikey"
    assert response.identity["match_type"] == "inchikey_derived_from_original_smiles"
    assert response.identity["lookup_value"] == INCHIKEY
    assert response.card["status"] == "hit"
    assert response.card["writes_selection"] is False
    assert (
        score.lipid_score,
        score.tox_risk,
        score.novelty_score,
        score.conf_e,
    ) == scientific_before
    assert result.selection_sha256 == selection_before
    assert selection_sha256(result.top_molecules) == selection_before


def test_direct_identity_returns_structured_snapshot_evidence(
    tmp_path, snapshot_dir, provider_config
) -> None:
    response = run_query_evidence(
        inchikey=INCHIKEY,
        providers=["chembl"],
        query_types=["lipid"],
        allow_live=False,
        **_query_paths(tmp_path, snapshot_dir, provider_config),
    )

    assert response.ok is True
    assert response.bundle.lipid_score == pytest.approx(0.72)
    assert response.bundle.conf_e == pytest.approx(0.61)
    item = next(
        row
        for row in response.card["evidence_items"]
        if row["evidence_id"] == "chembl:TEST:lipid"
    )
    assert item["query_status"] == "hit"
    assert item["provider"] == "chembl"
    assert item["lookup_field"] == "standardized_inchikey"
    assert item["lookup_value"] == INCHIKEY
    assert item["match_type"] == "exact_standardized_inchikey"
    assert item["participates_in_ranking"] is False
    assert item["claim_ceiling"]
    assert item["response_sha256"]
    required = {
        "evidence_id",
        "provider",
        "adapter_id",
        "query_type",
        "evidence_role",
        "evidence_type",
        "query_status",
        "lookup_field",
        "lookup_value",
        "match_type",
        "endpoint",
        "direction",
        "score",
        "confidence",
        "source_url",
        "accession",
        "retrieved_at",
        "source_version",
        "adapter_version",
        "response_sha256",
        "claim_ceiling",
    }
    for evidence_item in response.card["evidence_items"]:
        assert required <= evidence_item.keys()
        assert evidence_item["endpoint"]
        assert evidence_item["source_url"] or evidence_item["accession"]


def test_ranking_participation_requires_exact_frozen_evidence_signature(
    tmp_path, snapshot_dir, provider_config
) -> None:
    paths = _query_paths(tmp_path, snapshot_dir, provider_config)
    baseline = run_query_evidence(
        inchikey=INCHIKEY,
        providers=["chembl"],
        query_types=["lipid"],
        allow_live=False,
        **paths,
    )
    baseline_item = next(
        item
        for item in baseline.card["evidence_items"]
        if item["evidence_id"] == "chembl:TEST:lipid"
    )
    score = _score()
    consumed = EvidenceHit(
        adapter_id=baseline_item["adapter_id"],
        provider_id=baseline_item["provider"],
        query_type="lipid",
        score=baseline_item["score"],
        confidence=baseline_item["confidence"],
        evidence_id=baseline_item["evidence_id"],
        evidence_role="task_evidence",
        query_status="hit",
        response_sha256=baseline_item["response_sha256"],
        source_version=baseline_item["source_version"],
        adapter_version=baseline_item["adapter_version"],
    )
    score.evidence_hits = [consumed]
    result = _run_result(score)

    matched = run_query_evidence(
        result=result,
        molecule_id=MOLECULE_ID,
        providers=["chembl"],
        query_types=["lipid"],
        allow_live=False,
        **paths,
    )
    matched_item = next(
        item
        for item in matched.card["evidence_items"]
        if item["evidence_id"] == "chembl:TEST:lipid"
    )
    assert matched_item["participates_in_ranking"] is True
    assert matched_item["ranking_relation"] == "verified_frozen_scoring_input"

    consumed.source_version = "different-source-version"
    mismatched = run_query_evidence(
        result=result,
        molecule_id=MOLECULE_ID,
        providers=["chembl"],
        query_types=["lipid"],
        allow_live=False,
        **paths,
    )
    mismatched_item = next(
        item
        for item in mismatched.card["evidence_items"]
        if item["evidence_id"] == "chembl:TEST:lipid"
    )
    assert mismatched_item["participates_in_ranking"] is False


def test_missing_input_returns_audit_missing_without_guessing() -> None:
    events: list[dict[str, Any]] = []

    response = run_query_evidence(event_sink=events.append)

    assert response.ok is False
    assert response.error_code == "audit_missing"
    assert response.card["status"] == "audit_missing"
    assert response.bundle.lipid_score == 0.0
    assert response.bundle.tox_score == 0.0
    assert response.bundle.novelty_score == 0.0
    assert response.bundle.conf_e == 0.0
    assert {hit.query_status for hit in response.bundle.query_audit} == {
        "not_queried"
    }
    assert any(event["type"] == "query_summary" for event in events)


def test_unknown_current_run_molecule_id_is_explicit() -> None:
    response = run_query_evidence(
        result=_run_result(),
        molecule_id="T404",
    )

    assert response.ok is False
    assert response.error_code == "unknown_molecule_id"
    assert response.card["status"] == "audit_missing"
    assert response.identity["molecule_id"] == "T404"
    assert "未猜测" in response.message


def test_snapshot_hit_skips_live_provider_even_when_live_is_allowed(
    tmp_path, snapshot_dir, provider_config
) -> None:
    calls: list[Any] = []

    response = run_query_evidence(
        inchikey=INCHIKEY,
        providers=["chembl"],
        query_types=["lipid"],
        allow_live=True,
        provider_adapters={"chembl": _forbidden_adapter(calls)},
        **_query_paths(tmp_path, snapshot_dir, provider_config),
    )

    assert response.ok is True
    assert response.card["status"] == "hit"
    assert calls == []


def test_stale_frozen_empty_requeries_only_in_explicit_live_mode(
    tmp_path, provider_config
) -> None:
    snapshot = tmp_path / "stale-empty"
    snapshot.mkdir()
    row = {
        "inchikey": INCHIKEY,
        "adapter_id": "chembl_lipid_v1",
        "query_type": "query_audit",
        "score": 0.0,
        "confidence": 0.0,
        "evidence_id": "chembl:stale:empty",
        "payload": {"reason": "no_provider_record"},
        "endpoint": "candidate_activity",
        "evidence_role": "query_audit",
        "query_status": "verified_empty",
        "retrieved_at": "2000-01-01T00:00:00+00:00",
        "adapter_version": "chembl-test-v1",
        "source_version": "chembl-test-source-v1",
        "response_sha256": "e" * 64,
        "schema_version": "evidence-v2",
    }
    (snapshot / "empty.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    calls: list[Any] = []

    def live(task):
        calls.append(task)
        return [
            EvidenceHit(
                adapter_id="chembl",
                provider_id="chembl",
                query_type="lipid",
                score=0.6,
                confidence=0.5,
                evidence_id="chembl:refreshed:lipid",
                payload={"chembl_id": "CHEMBL_REFRESHED"},
                endpoint="cellular_lipid_reduction",
                direction="supports",
                evidence_role="task_evidence",
                query_status="hit",
            )
        ]

    offline = run_query_evidence(
        inchikey=INCHIKEY,
        providers=["chembl"],
        allow_live=False,
        provider_adapters={"chembl": live},
        **_query_paths(tmp_path, snapshot, provider_config),
    )
    refreshed = run_query_evidence(
        inchikey=INCHIKEY,
        providers=["chembl"],
        allow_live=True,
        provider_adapters={"chembl": live},
        **_query_paths(tmp_path, snapshot, provider_config),
    )

    assert calls and len(calls) == 1
    assert offline.card["provider_statuses"]["chembl"]["status"] == "verified_empty"
    assert refreshed.card["provider_statuses"]["chembl"]["status"] == "hit"
    assert refreshed.bundle.lipid_score == pytest.approx(0.6)


def test_force_refresh_requeries_fresh_frozen_hit(tmp_path, snapshot_dir, provider_config) -> None:
    calls: list[Any] = []

    def live(task):
        calls.append(task)
        return []

    response = run_query_evidence(
        inchikey=INCHIKEY,
        providers=["chembl"],
        query_types=["lipid"],
        allow_live=True,
        force_refresh=True,
        provider_adapters={"chembl": live},
        **_query_paths(tmp_path, snapshot_dir, provider_config),
    )

    assert len(calls) == 1
    assert response.bundle.lipid_score == pytest.approx(0.72)
    statuses = response.card["provider_statuses"]["chembl"]["statuses"]
    assert "hit" in statuses
    assert "verified_empty" in statuses


def test_allow_live_false_and_force_refresh_never_call_provider(
    tmp_path, provider_config
) -> None:
    empty_snapshot = tmp_path / "empty-snapshot"
    empty_snapshot.mkdir()
    calls: list[Any] = []

    response = run_query_evidence(
        inchikey=INCHIKEY,
        providers=["chembl"],
        query_types=["lipid"],
        allow_live=False,
        force_refresh=True,
        provider_adapters={"chembl": _forbidden_adapter(calls)},
        **_query_paths(tmp_path, empty_snapshot, provider_config),
    )

    assert response.ok is False
    assert response.error_code == "not_queried"
    assert response.card["allow_live"] is False
    assert response.card["provider_statuses"]["chembl"]["status"] == "not_queried"
    assert calls == []


def test_identity_conflict_is_audit_only_and_cannot_raise_scores(
    tmp_path, provider_config
) -> None:
    empty_snapshot = tmp_path / "empty-snapshot"
    empty_snapshot.mkdir()
    calls: list[Any] = []

    response = run_query_evidence(
        # Ethanol InChIKey with propane SMILES: deterministic identity conflict.
        inchikey=INCHIKEY,
        smiles="CCC",
        providers=["chembl"],
        allow_live=True,
        provider_adapters={"chembl": _forbidden_adapter(calls)},
        **_query_paths(tmp_path, empty_snapshot, provider_config),
    )

    assert response.ok is False
    assert response.error_code == "identity_review_required"
    assert response.card["status"] == "identity_review_required"
    assert response.bundle.has_identity_review_required is True
    assert response.bundle.lipid_score == 0.0
    assert response.bundle.tox_score == 0.0
    assert response.bundle.novelty_score == 0.0
    assert response.bundle.conf_e == 0.0
    assert all(
        hit.score == 0.0 and hit.confidence == 0.0
        for hit in response.bundle.all_hits()
    )
    assert calls == []


def test_final_bundle_gates_snapshot_live_provider_identity_drift(tmp_path) -> None:
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()
    snapshot_identity = "CHEMBL_SNAPSHOT"
    common = {
        "inchikey": INCHIKEY,
        "adapter_id": "chembl_lipid_v1",
        "payload": {"chembl_id": snapshot_identity},
        "evidence_role": "task_evidence",
        "query_status": "hit",
        "schema_version": "evidence-v2",
        "source_url": "https://example.test/frozen",
        "retrieved_at": "2026-01-01T00:00:00+00:00",
        "adapter_version": "chembl-frozen-v1",
        "source_version": "chembl-frozen-v1",
        "response_sha256": "a" * 64,
    }
    rows = [
        {
            **common,
            "query_type": "lipid",
            "score": 0.72,
            "confidence": 0.61,
            "evidence_id": "chembl:frozen:lipid",
            "endpoint": "cellular_lipid_reduction",
            "direction": "supports",
        },
        {
            **common,
            "query_type": "tox",
            "score": 0.88,
            "confidence": 0.80,
            "evidence_id": "chembl:frozen:safety",
            "endpoint": "safety_annotation",
            "direction": "supports_safety",
        },
        {
            **common,
            "query_type": "novelty",
            "score": 0.91,
            "confidence": 0.90,
            "evidence_id": "chembl:frozen:novelty",
            "endpoint": "novelty_annotation",
            "direction": "supports",
        },
    ]
    (snapshot_dir / "fixture.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    provider_config = tmp_path / "evidence_providers.yaml"
    provider_config.write_text(
        """
cache:
  ttl_days: {hit: 90, annotation_only: 30, verified_empty: 14}
providers:
  chembl:
    enabled: true
    identity_order: [standardized_inchikey]
    endpoint: candidate_bundle
    query_types:
      lipid: {endpoint: candidate_bundle}
      tox: {endpoint: candidate_bundle}
      novelty: {endpoint: candidate_bundle}
    adapter_version: chembl-live-v2
    concurrency: 1
    timeout_sec: 1
    retry_attempts: 0
""".lstrip(),
        encoding="utf-8",
    )
    calls: list[Any] = []

    def live_adapter(task):
        calls.append(task)
        return [
            EvidenceHit(
                adapter_id="chembl",
                provider_id="chembl",
                query_type="lipid",
                score=0.95,
                confidence=0.92,
                evidence_id="chembl:live:lipid",
                payload={"chembl_id": "CHEMBL_LIVE"},
                endpoint="cellular_lipid_reduction",
                direction="supports",
                evidence_role="task_evidence",
                query_status="hit",
                source_url="https://example.test/live",
                retrieved_at="2026-07-27T00:00:00+00:00",
                adapter_version="chembl-live-v2",
                source_version="chembl-live-v2",
                response_sha256="b" * 64,
            )
        ]

    response = run_query_evidence(
        inchikey=INCHIKEY,
        providers=["chembl"],
        query_types=["lipid", "tox", "novelty"],
        allow_live=True,
        force_refresh=True,
        provider_adapters={"chembl": live_adapter},
        snapshot_dir=snapshot_dir,
        provider_config_path=provider_config,
        cache_path=tmp_path / "query-state.sqlite",
    )

    assert len(calls) == 1
    assert response.ok is False
    assert response.error_code == "identity_review_required"
    assert response.card["status"] == "identity_review_required"
    assert response.bundle.has_identity_review_required is True
    assert response.bundle.lipid_score == 0.0
    assert response.bundle.conf_e == 0.0
    assert response.bundle.tox_score == 0.0
    assert response.bundle.novelty_score == 0.0
    assert response.bundle.has_safety_clearance_evidence is False

    conflicts = response.card["identity_conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["provider"] == "chembl"
    assert conflicts[0]["reason"] == "provider_compound_identity_conflict"
    assert set(conflicts[0]["claims"]) == {
        "chembl_id:CHEMBL_LIVE",
        "chembl_id:CHEMBL_SNAPSHOT",
    }
    reviews = [
        item
        for item in response.card["evidence_items"]
        if item["provider"] == "chembl"
        and item["query_status"] == "identity_review_required"
    ]
    assert len(reviews) == 1
    assert reviews[0]["evidence_role"] == "query_audit"
    assert reviews[0]["score"] == reviews[0]["confidence"] == 0.0
