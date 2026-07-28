from __future__ import annotations

import json
import time

from packages.models import EvidenceHit
from plugins.molmind_core.scientific.evidence_facade.bundle import EvidenceBundle
from services.evidence_gateway import (
    EvidenceQueryCache,
    EvidenceRetriever,
    resolve_identity,
)

INCHIKEY = "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"


def _provider(
    *query_types: str,
    endpoint: str = "bundle",
    concurrency: int = 2,
) -> dict:
    return {
        "identity_order": ["original_inchikey", "standardized_inchikey", "cas"],
        "query_types": {
            query_type: {"endpoint": endpoint} for query_type in query_types
        },
        "concurrency": concurrency,
        "timeout_sec": 1,
        "retry_attempts": 0,
        "circuit_fail_threshold": 2,
        "circuit_reset_sec": 10,
        "adapter_version": "test-v1",
    }


def _hit(provider: str, evidence_id: str, query_type: str = "lipid") -> EvidenceHit:
    return EvidenceHit(
        adapter_id=provider,
        query_type=query_type,
        score=0.7,
        confidence=0.8,
        evidence_id=evidence_id,
        direction="supports",
        evidence_role="task_evidence",
        query_status="hit",
    )


def _provider_audit(provider: str, evidence_id: str, status: str) -> EvidenceHit:
    return EvidenceHit(
        adapter_id=provider,
        provider_id=provider,
        query_type="query_audit",
        score=0.9,
        confidence=0.9,
        evidence_id=evidence_id,
        evidence_role="query_audit",
        query_status=status,
        payload={"provider_status": status},
    )


def test_same_run_duplicate_request_is_executed_once_and_reassociated(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    calls: list[str] = []

    def adapter(task):
        calls.append(task.lookup_value)
        return [_hit("chembl", "lipid:1"), _hit("chembl", "tox:1", "tox")]

    retriever = EvidenceRetriever(
        cache,
        {"providers": {"chembl": _provider("lipid", "tox")}},
        {"chembl": adapter},
    )
    try:
        result = retriever.query(
            [
                {"molecule_id": "M1", "original_inchikey": INCHIKEY},
                {"molecule_id": "M1-alias", "original_inchikey": INCHIKEY},
            ],
            allow_live=True,
        )
        assert calls == [INCHIKEY]
        assert [hit.evidence_id for hit in result.hits_by_molecule["M1"]] == [
            "lipid:1",
            "tox:1",
        ]
        assert [hit.evidence_id for hit in result.hits_by_molecule["M1-alias"]] == [
            "lipid:1",
            "tox:1",
        ]
        assert len(result.tasks) == 2

        # A repeat call is served by the fresh query-state payload.
        repeated = retriever.query(
            [{"molecule_id": "M1", "original_inchikey": INCHIKEY}],
            allow_live=True,
        )
        assert calls == [INCHIKEY]
        assert repeated.tasks[0].action == "local_hit"
    finally:
        cache.close()


def test_allow_live_false_never_calls_adapter(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    called = False

    def adapter(task):
        nonlocal called
        called = True
        return [_hit("chembl", "unexpected")]

    retriever = EvidenceRetriever(
        cache,
        {"providers": {"chembl": _provider("lipid")}},
        {"chembl": adapter},
    )
    try:
        result = retriever.query(
            [{"molecule_id": "M1", "original_inchikey": INCHIKEY}],
            allow_live=False,
        )
        assert called is False
        assert result.hits_by_molecule["M1"] == []
        assert result.audits_by_molecule["M1"][0].query_status == "not_queried"
        assert result.events[0]["event"] == "query_plan"
        assert result.events[0]["allow_live"] is False
    finally:
        cache.close()


def test_fresh_verified_empty_is_not_requeried_or_biological_negative(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    calls = 0

    def adapter(task):
        nonlocal calls
        calls += 1
        return []

    retriever = EvidenceRetriever(
        cache,
        {"providers": {"pubchem": _provider("lipid")}},
        {"pubchem": adapter},
    )
    try:
        first = retriever.query(
            [{"molecule_id": "M1", "original_inchikey": INCHIKEY}],
            allow_live=True,
        )
        second = retriever.query(
            [{"molecule_id": "M1", "original_inchikey": INCHIKEY}],
            allow_live=True,
        )
        assert calls == 1
        assert first.audits_by_molecule["M1"][0].query_status == "verified_empty"
        audit = second.audits_by_molecule["M1"][0]
        assert audit.query_status == "verified_empty"
        assert audit.payload["verified_empty_is_not_biological_negative"] is True
        assert second.tasks[0].action == "skip_fresh_verified_empty"
    finally:
        cache.close()


def test_stale_verified_empty_requeries_when_live(tmp_path) -> None:
    cache = EvidenceQueryCache(
        tmp_path / "state.sqlite", {"ttl_days": {"verified_empty": 0}}
    )
    calls = 0

    def adapter(task):
        nonlocal calls
        calls += 1
        return []

    retriever = EvidenceRetriever(
        cache,
        {
            "cache": {"ttl_days": {"verified_empty": 0}},
            "providers": {"pubchem": _provider("lipid")},
        },
        {"pubchem": adapter},
    )
    try:
        retriever.query(
            [{"molecule_id": "M1", "original_inchikey": INCHIKEY}],
            allow_live=True,
        )
        retriever.query(
            [{"molecule_id": "M1", "original_inchikey": INCHIKEY}],
            allow_live=True,
        )
        assert calls == 2
    finally:
        cache.close()


def test_provider_failure_is_isolated_and_credentials_are_redacted(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")

    def broken(task):
        raise RuntimeError("Authorization: Bearer secret-value")

    def healthy(task):
        return [_hit("healthy", "healthy:1")]

    retriever = EvidenceRetriever(
        cache,
        {
            "providers": {
                "broken": _provider("lipid"),
                "healthy": _provider("lipid"),
            }
        },
        {"broken": broken, "healthy": healthy},
    )
    try:
        result = retriever.query(
            [{"molecule_id": "M1", "original_inchikey": INCHIKEY}],
            allow_live=True,
        )
        assert result.degraded_channels == ["broken"]
        assert [hit.evidence_id for hit in result.hits_by_molecule["M1"]] == [
            "healthy:1"
        ]
        assert [hit.query_status for hit in result.audits_by_molecule["M1"]] == [
            "auth_missing",
            "hit",
        ]
        assert "secret-value" not in json.dumps(result.events)
        event_types = [event["type"] for event in result.events]
        assert "remote_start" in event_types
        assert "remote_end" in event_types
        assert event_types[-1] == "query_summary"
        remote_start = next(event for event in result.events if event["type"] == "remote_start")
        assert remote_start["provider"] == remote_start["provider_id"] == "broken"
        state = cache.get_state(
            source_id="broken", entity_key=INCHIKEY, endpoint="bundle"
        )
        assert state is not None
        assert "secret-value" not in state["error_message"]
    finally:
        cache.close()


def test_provider_verified_empty_audit_is_not_rewritten_as_hit(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")

    def adapter(task):
        return [_provider_audit("chembl", "chembl:empty", "verified_empty")]

    retriever = EvidenceRetriever(
        cache,
        {"providers": {"chembl": _provider("lipid")}},
        {"chembl": adapter},
    )
    try:
        result = retriever.query(
            [{"molecule_id": "M1", "original_inchikey": INCHIKEY}],
            query_types=["lipid"],
            allow_live=True,
        )
        assert result.hits_by_molecule["M1"] == []
        statuses = [hit.query_status for hit in result.audits_by_molecule["M1"]]
        assert statuses == ["verified_empty", "verified_empty"]
        assert all(hit.score == hit.confidence == 0.0 for hit in result.audits_by_molecule["M1"])
        state = cache.get_state(
            source_id="chembl", entity_key=INCHIKEY, endpoint="bundle"
        )
        assert state is not None and state["status"] == "verified_empty"
    finally:
        cache.close()


def test_partial_provider_failure_stays_degraded_on_cache_replay(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    calls = 0

    def adapter(_task):
        nonlocal calls
        calls += 1
        return [
            _hit("chembl", "chembl:partial:hit"),
            _provider_audit(
                "chembl", "chembl:partial:failure", "query_failed"
            ),
        ]

    retriever = EvidenceRetriever(
        cache,
        {"providers": {"chembl": _provider("lipid")}},
        {"chembl": adapter},
    )
    try:
        first = retriever.query(
            [{"molecule_id": "M1", "original_inchikey": INCHIKEY}],
            allow_live=True,
        )
        replay = retriever.query(
            [{"molecule_id": "M1", "original_inchikey": INCHIKEY}],
            allow_live=True,
        )
        assert calls == 1
        assert first.degraded_channels == replay.degraded_channels == ["chembl"]
        assert [hit.evidence_id for hit in first.hits_by_molecule["M1"]] == [
            "chembl:partial:hit"
        ]
        assert [hit.evidence_id for hit in replay.hits_by_molecule["M1"]] == [
            "chembl:partial:hit"
        ]
        assert any(
            hit.evidence_id == "chembl:partial:failure"
            and hit.query_status == "query_failed"
            for hit in replay.audits_by_molecule["M1"]
        )
        state = cache.get_state(
            source_id="chembl", entity_key=INCHIKEY, endpoint="bundle"
        )
        assert state is not None and state["status"] == "hit"
    finally:
        cache.close()


def test_requested_type_filter_preserves_annotation_and_identity_review(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")

    def adapter(task):
        return [
            _provider_audit(
                "epa_ctx", "epa:identity-review", "identity_review_required"
            ),
            EvidenceHit(
                adapter_id="epa_ctx",
                query_type="annotation",
                score=0.8,
                confidence=0.8,
                evidence_id="epa:identity",
                evidence_role="annotation_only",
                query_status="annotation_only",
            ),
        ]

    retriever = EvidenceRetriever(
        cache,
        {"providers": {"epa_ctx": _provider("lipid")}},
        {"epa_ctx": adapter},
    )
    try:
        result = retriever.query(
            [{"molecule_id": "M1", "original_inchikey": INCHIKEY}],
            query_types=["lipid"],
            allow_live=True,
        )
        assert [hit.evidence_id for hit in result.hits_by_molecule["M1"]] == [
            "epa:identity"
        ]
        assert result.hits_by_molecule["M1"][0].score == 0.0
        assert any(
            hit.evidence_id == "epa:identity-review"
            and hit.query_status == "identity_review_required"
            for hit in result.audits_by_molecule["M1"]
        )
        state = cache.get_state(
            source_id="epa_ctx", entity_key=INCHIKEY, endpoint="bundle"
        )
        assert state is not None and state["status"] == "identity_review_required"
    finally:
        cache.close()


def test_concurrent_completion_does_not_change_provider_order(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")

    def slow(task):
        time.sleep(0.03)
        return [_hit("slow", "slow:1")]

    def fast(task):
        return [_hit("fast", "fast:1")]

    retriever = EvidenceRetriever(
        cache,
        {
            "providers": {
                "slow": _provider("lipid"),
                "fast": _provider("lipid"),
            }
        },
        {"slow": slow, "fast": fast},
    )
    try:
        result = retriever.query(
            [{"molecule_id": "M1", "original_inchikey": INCHIKEY}],
            allow_live=True,
        )
        assert [task.provider_id for task in result.tasks] == ["slow", "fast"]
        assert [hit.evidence_id for hit in result.hits_by_molecule["M1"]] == [
            "slow:1",
            "fast:1",
        ]
    finally:
        cache.close()


def test_identity_conflict_is_a_non_scoring_audit_and_skips_adapter(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    calls = 0

    def adapter(task):
        nonlocal calls
        calls += 1
        return [_hit("chembl", "unexpected")]

    retriever = EvidenceRetriever(
        cache,
        {"providers": {"chembl": _provider("lipid")}},
        {"chembl": adapter},
    )
    try:
        identity = resolve_identity(
            molecule_id="M1",
            standardized_inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
            smiles="CCC",
        )
        result = retriever.query([identity], allow_live=True)
        assert calls == 0
        assert result.hits_by_molecule["M1"] == []
        audit = result.audits_by_molecule["M1"][0]
        assert audit.query_status == "identity_review_required"
        assert audit.score == audit.confidence == 0.0
        assert any(event["event"] == "identity_conflict" for event in result.events)
    finally:
        cache.close()


def test_query_audit_and_annotation_cannot_change_bundle_scores() -> None:
    audit = EvidenceHit(
        adapter_id="gateway",
        query_type="query_audit",
        score=1.0,
        confidence=1.0,
        evidence_id="audit:1",
        evidence_role="query_audit",
        query_status="hit",
    )
    annotation = EvidenceHit(
        adapter_id="chembl",
        query_type="novelty",
        score=1.0,
        confidence=1.0,
        evidence_id="annotation:1",
        evidence_role="annotation_only",
        query_status="annotation_only",
    )
    bundle = EvidenceBundle(
        lipid=[audit],
        tox=[audit],
        novelty=[audit, annotation],
        query_audit=[audit],
        annotation=[annotation],
    )
    assert bundle.lipid_score == 0.0
    assert bundle.tox_score == 0.0
    assert bundle.novelty_score == 0.0
    assert bundle.conf_e == 0.0
    assert bundle.has_any is False


def test_non_hit_transport_states_cannot_score_even_with_task_role() -> None:
    for status in (
        "verified_empty",
        "query_failed",
        "auth_missing",
        "not_queried",
        "annotation_only",
        "identity_review_required",
    ):
        incoherent = EvidenceHit(
            adapter_id="bad-provider",
            query_type="lipid",
            score=0.95,
            confidence=0.95,
            evidence_id=f"bad:{status}",
            evidence_role="task_evidence",
            query_status=status,
        )
        bundle = EvidenceBundle(
            lipid=[incoherent],
            tox=[incoherent],
            novelty=[incoherent],
            pathway=[incoherent],
        )
        assert bundle.lipid_score == 0.0
        assert bundle.tox_score == 0.0
        assert bundle.novelty_score == 0.0
        assert bundle.conf_e == 0.0
        assert bundle.has_any is False


def test_identity_review_blocks_benefit_and_safety_lift_but_keeps_risk() -> None:
    lipid = _hit("chembl", "lipid:benefit", "lipid")
    novelty = _hit("novelty", "novelty:benefit", "novelty")
    safety = _hit("safety", "tox:safety", "tox")
    safety.score = 0.95
    safety.direction = "supports_safety"
    risk = _hit("risk", "tox:risk", "tox")
    risk.score = 0.64
    risk.confidence = 0.7
    risk.direction = "risk"
    review = EvidenceHit(
        adapter_id="identity",
        query_type="query_audit",
        score=0.0,
        confidence=0.0,
        evidence_id="identity:review",
        evidence_role="query_audit",
        query_status="identity_review_required",
    )
    bundle = EvidenceBundle(
        lipid=[lipid],
        tox=[safety, risk],
        novelty=[novelty],
        query_audit=[review],
    )

    assert bundle.lipid_score == 0.0
    assert bundle.conf_e == 0.0
    assert bundle.novelty_score == 0.0
    assert bundle.has_safety_clearance_evidence is False
    assert bundle.toxicity_evidence_coverage == 0.7
    assert bundle.tox_score == 0.64


def test_single_worker_queue_does_not_consume_timeout_before_dispatch(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    calls: list[str] = []

    def adapter(task):
        calls.append(task.lookup_value)
        time.sleep(0.03)
        return [_hit("chembl", f"hit:{task.lookup_value}")]

    provider = _provider("lipid", concurrency=1)
    provider["timeout_sec"] = 0.08
    retriever = EvidenceRetriever(
        cache,
        {"providers": {"chembl": provider}},
        {"chembl": adapter},
    )
    identities = [
        {"molecule_id": "M1", "original_inchikey": INCHIKEY},
        {
            "molecule_id": "M2",
            "original_inchikey": "OKKJLVBELUTLKV-UHFFFAOYSA-N",
        },
        {
            "molecule_id": "M3",
            "original_inchikey": "CSCPPACGZOOCGX-UHFFFAOYSA-N",
        },
    ]
    try:
        result = retriever.query(identities, allow_live=True)
        assert len(calls) == 3
        assert all(result.hits_by_molecule[f"M{index}"] for index in range(1, 4))
        assert result.degraded_channels == []
    finally:
        cache.close()


def test_circuit_breaker_stops_unsent_provider_requests(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    calls = 0

    def adapter(_task):
        nonlocal calls
        calls += 1
        raise RuntimeError("provider failed")

    provider = _provider("lipid", concurrency=1)
    provider["circuit_fail_threshold"] = 1
    retriever = EvidenceRetriever(
        cache,
        {"providers": {"chembl": provider}},
        {"chembl": adapter},
    )
    identities = [
        {"molecule_id": "M1", "original_inchikey": INCHIKEY},
        {
            "molecule_id": "M2",
            "original_inchikey": "OKKJLVBELUTLKV-UHFFFAOYSA-N",
        },
        {
            "molecule_id": "M3",
            "original_inchikey": "CSCPPACGZOOCGX-UHFFFAOYSA-N",
        },
    ]
    try:
        result = retriever.query(identities, allow_live=True)
        assert calls == 1
        statuses = [
            result.audits_by_molecule[f"M{index}"][-1].query_status
            for index in range(1, 4)
        ]
        assert statuses == ["query_failed", "not_queried", "not_queried"]
        starts = [event for event in result.events if event["type"] == "remote_start"]
        ends = [event for event in result.events if event["type"] == "remote_end"]
        assert len(starts) == len(ends) == 1
    finally:
        cache.close()


def test_incoherent_annotation_is_zeroed_and_bad_numeric_is_isolated(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")

    def annotation(_task):
        hit = _hit("annotation", "annotation:1")
        hit.query_status = "annotation_only"
        hit.score = 0.95
        hit.confidence = 0.95
        return [hit]

    def broken_numeric(_task):
        hit = _hit("broken", "broken:1")
        hit.score = "not-a-number"  # type: ignore[assignment]
        return [hit]

    def healthy(_task):
        return [_hit("healthy", "healthy:1")]

    retriever = EvidenceRetriever(
        cache,
        {
            "providers": {
                "annotation": _provider("lipid"),
                "broken": _provider("lipid"),
                "healthy": _provider("lipid"),
            }
        },
        {
            "annotation": annotation,
            "broken": broken_numeric,
            "healthy": healthy,
        },
    )
    try:
        result = retriever.query(
            [{"molecule_id": "M1", "original_inchikey": INCHIKEY}],
            allow_live=True,
        )
        by_id = {
            hit.evidence_id: hit for hit in result.hits_by_molecule["M1"]
        }
        assert by_id["annotation:1"].evidence_role == "annotation_only"
        assert by_id["annotation:1"].score == 0.0
        assert by_id["annotation:1"].confidence == 0.0
        assert by_id["healthy:1"].query_status == "hit"
        assert "broken" in result.degraded_channels
        assert any(
            hit.provider_id == "broken" and hit.query_status == "query_failed"
            for hit in result.audits_by_molecule["M1"]
        )
    finally:
        cache.close()


def test_provider_lookup_metadata_conflict_is_identity_review(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")

    def adapter(_task):
        hit = _hit("chembl", "foreign:1")
        hit.provider_id = "chembl"
        hit.lookup_field = "original_inchikey"
        hit.lookup_value = "OKKJLVBELUTLKV-UHFFFAOYSA-N"
        return [hit]

    retriever = EvidenceRetriever(
        cache,
        {"providers": {"chembl": _provider("lipid")}},
        {"chembl": adapter},
    )
    try:
        result = retriever.query(
            [{"molecule_id": "M1", "original_inchikey": INCHIKEY}],
            allow_live=True,
        )
        assert result.hits_by_molecule["M1"] == []
        review = next(
            hit
            for hit in result.audits_by_molecule["M1"]
            if hit.query_status == "identity_review_required"
        )
        assert review.score == review.confidence == 0.0
        assert review.payload["reason"] == "provider_lookup_metadata_conflict"
    finally:
        cache.close()
