from __future__ import annotations

from datetime import timedelta

from services.evidence_gateway.cache import EvidenceQueryCache


def test_unseen_entity_is_remote_only_when_online(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        online = cache.decide(source_id="epa_ctx", entity_key="IK1", endpoint="identity", online=True)
        offline = cache.decide(source_id="epa_ctx", entity_key="IK2", endpoint="identity", online=False)
        assert online.action == "query_remote"
        assert online.status == "not_queried"
        assert offline.action == "offline_missing"
    finally:
        cache.close()


def test_hit_reads_local_payload(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        cache.record(
            source_id="chembl",
            entity_key="IK1",
            endpoint="activity",
            status="hit",
            ttl=timedelta(days=1),
            payload_path="objects/sha256.json",
        )
        decision = cache.decide(source_id="chembl", entity_key="IK1", endpoint="activity", online=True)
        assert decision.action == "local_hit"
        assert decision.payload_path == "objects/sha256.json"
    finally:
        cache.close()


def test_hit_can_store_small_normalized_payload_inline(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        cache.record(
            source_id="epa_ctx",
            entity_key="IK1",
            endpoint="identity",
            status="hit",
            ttl=timedelta(days=1),
            payload={"dtxsid": "DTXSID1"},
        )
        decision = cache.decide(source_id="epa_ctx", entity_key="IK1", endpoint="identity", online=False)
        assert decision.action == "local_hit"
        assert cache.load_payload(source_id="epa_ctx", entity_key="IK1", endpoint="identity") == {
            "dtxsid": "DTXSID1"
        }
        state = cache.get_state(source_id="epa_ctx", entity_key="IK1", endpoint="identity")
        assert state is not None
        assert state["status"] == "hit"
        assert state["attempt_count"] == 1
    finally:
        cache.close()


def test_verified_empty_is_a_finite_negative_cache_not_a_biological_label(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        cache.record(
            source_id="pubchem",
            entity_key="IK1",
            endpoint="compound",
            status="verified_empty",
            ttl=timedelta(days=1),
        )
        decision = cache.decide(source_id="pubchem", entity_key="IK1", endpoint="compound", online=True)
        assert decision.action == "skip_fresh_verified_empty"
        assert "not a biological negative" in decision.reason
    finally:
        cache.close()


def test_stale_empty_and_failed_backoff_are_retryable(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        cache.record(
            source_id="pubchem",
            entity_key="IK1",
            endpoint="compound",
            status="verified_empty",
            ttl=timedelta(seconds=-1),
        )
        stale = cache.decide(source_id="pubchem", entity_key="IK1", endpoint="compound", online=True)
        assert stale.action == "query_remote"

        cache.record(
            source_id="epa_ctx",
            entity_key="IK2",
            endpoint="toxicity",
            status="query_failed",
            retry_after=timedelta(days=1),
        )
        blocked = cache.decide(source_id="epa_ctx", entity_key="IK2", endpoint="toxicity", online=True)
        assert blocked.action == "offline_missing"
        assert blocked.status == "query_failed"
    finally:
        cache.close()
