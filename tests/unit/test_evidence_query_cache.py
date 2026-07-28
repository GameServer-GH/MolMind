from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    object_path = tmp_path / "objects" / "sha256.json"
    object_path.parent.mkdir()
    object_path.write_text('{"hits":[{"evidence_id":"cached:1"}]}', encoding="utf-8")
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
        assert cache.load_payload(
            source_id="chembl", entity_key="IK1", endpoint="activity"
        ) == {"hits": [{"evidence_id": "cached:1"}]}
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


def test_force_refresh_requeries_fresh_annotation_only_when_online(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        cache.record(
            source_id="epa_ctx",
            entity_key="IK1",
            endpoint="identity",
            status="annotation_only",
            ttl=timedelta(days=1),
            payload={"dtxsid": "DTXSID1"},
        )
        normal = cache.decide(
            source_id="epa_ctx",
            entity_key="IK1",
            endpoint="identity",
            online=True,
        )
        forced = cache.decide(
            source_id="epa_ctx",
            entity_key="IK1",
            endpoint="identity",
            online=True,
            force_refresh=True,
        )
        offline_forced = cache.decide(
            source_id="epa_ctx",
            entity_key="IK1",
            endpoint="identity",
            online=False,
            force_refresh=True,
        )
        assert normal.action == "local_hit"
        assert forced.action == "query_remote"
        assert offline_forced.action == "local_hit"
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


def test_failure_and_auth_backoff_are_not_bypassed_by_force_refresh(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        for source_id, status in (
            ("chembl", "query_failed"),
            ("epa_ctx", "auth_missing"),
        ):
            cache.record(
                source_id=source_id,
                entity_key="IK1",
                endpoint="activity",
                status=status,
                retry_after=timedelta(hours=1),
            )
            decision = cache.decide(
                source_id=source_id,
                entity_key="IK1",
                endpoint="activity",
                online=True,
                force_refresh=True,
            )
            assert decision.action == "offline_missing"
            assert decision.status == status
            assert "backoff" in decision.reason
    finally:
        cache.close()


def test_force_refresh_bypasses_fresh_hit_and_empty_only_online(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        cache.record(
            source_id="chembl",
            entity_key="IK1",
            endpoint="activity",
            status="hit",
            payload={"hits": []},
        )
        cache.record(
            source_id="pubchem",
            entity_key="IK1",
            endpoint="compound",
            status="verified_empty",
        )
        assert cache.decide(
            source_id="chembl",
            entity_key="IK1",
            endpoint="activity",
            online=True,
            force_refresh=True,
        ).action == "query_remote"
        assert cache.decide(
            source_id="pubchem",
            entity_key="IK1",
            endpoint="compound",
            online=True,
            force_refresh=True,
        ).action == "query_remote"
        assert cache.decide(
            source_id="chembl",
            entity_key="IK1",
            endpoint="activity",
            online=False,
            force_refresh=True,
        ).action == "local_hit"
    finally:
        cache.close()


def test_failed_refresh_preserves_previous_payload_and_version(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        cache.record(
            source_id="chembl",
            entity_key="IK1",
            endpoint="activity",
            status="hit",
            payload={"hits": [{"evidence_id": "known"}]},
            source_version="chembl-34",
            adapter_version="chembl-adapter-v3",
        )
        before = cache.get_state(
            source_id="chembl", entity_key="IK1", endpoint="activity"
        )
        cache.record(
            source_id="chembl",
            entity_key="IK1",
            endpoint="activity",
            status="query_failed",
            error=RuntimeError("Authorization: Bearer top-secret-token"),
        )
        after = cache.get_state(
            source_id="chembl", entity_key="IK1", endpoint="activity"
        )
        assert before is not None and after is not None
        assert after["payload_sha256"] == before["payload_sha256"]
        assert after["source_version"] == "chembl-34"
        assert after["adapter_version"] == "chembl-adapter-v3"
        assert cache.load_payload(
            source_id="chembl", entity_key="IK1", endpoint="activity"
        ) == {"hits": [{"evidence_id": "known"}]}
        assert "top-secret-token" not in after["error_message"]
        assert "[REDACTED]" in after["error_message"]
    finally:
        cache.close()


def test_stale_verified_empty_retries_at_explicit_clock(tmp_path) -> None:
    cache = EvidenceQueryCache(
        tmp_path / "state.sqlite",
        {"ttl_days": {"verified_empty": 0}},
    )
    try:
        cache.record(
            source_id="pubchem",
            entity_key="IK1",
            endpoint="compound",
            status="verified_empty",
        )
        decision = cache.decide(
            source_id="pubchem",
            entity_key="IK1",
            endpoint="compound",
            online=True,
            now=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
        assert decision.action == "query_remote"
    finally:
        cache.close()


def test_tampered_inline_hit_payload_is_not_replayed_as_success(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        cache.record(
            source_id="chembl",
            entity_key="IK1",
            endpoint="activity",
            status="hit",
            payload={"hits": [{"evidence_id": "known"}]},
        )
        cache.db.execute(
            "UPDATE source_query SET payload_json=? WHERE source_id=? AND entity_key=? AND endpoint=?",
            ('{"hits":[{"evidence_id":"tampered"}]}', "chembl", "IK1", "activity"),
        )
        cache.db.commit()

        assert cache.load_payload(
            source_id="chembl", entity_key="IK1", endpoint="activity"
        ) is None
        offline = cache.decide(
            source_id="chembl", entity_key="IK1", endpoint="activity", online=False
        )
        online = cache.decide(
            source_id="chembl", entity_key="IK1", endpoint="activity", online=True
        )
        assert offline.action == "offline_missing"
        assert offline.status == "query_failed"
        assert online.action == "retry_remote"
    finally:
        cache.close()


def test_missing_expiry_and_changed_query_contract_are_stale(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        cache.record(
            source_id="chembl",
            entity_key="IK1",
            endpoint="activity",
            status="hit",
            payload={"hits": [{"evidence_id": "known"}]},
            adapter_version="adapter-v1",
            endpoint_url="https://example.test/v1",
        )
        cache.db.execute(
            "UPDATE source_query SET expires_at=NULL WHERE source_id=? AND entity_key=? AND endpoint=?",
            ("chembl", "IK1", "activity"),
        )
        cache.db.commit()
        stale = cache.decide(
            source_id="chembl", entity_key="IK1", endpoint="activity", online=True
        )
        assert stale.action == "query_remote"

        mismatch = cache.decide(
            source_id="chembl",
            entity_key="IK1",
            endpoint="activity",
            online=False,
            expected_adapter_version="adapter-v2",
            expected_endpoint_url="https://example.test/v2",
        )
        assert mismatch.action == "offline_missing"
        assert mismatch.status == "not_queried"
        assert "adapter_version" in mismatch.reason
        assert "endpoint_url" in mismatch.reason
    finally:
        cache.close()


def test_plaintext_credential_and_private_key_are_redacted(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        cache.record(
            source_id="epa_ctx",
            entity_key="IK1",
            endpoint="identity",
            status="query_failed",
            error=RuntimeError(
                "credential=do-not-store private_key=also-secret"
            ),
        )
        state = cache.get_state(
            source_id="epa_ctx", entity_key="IK1", endpoint="identity"
        )
        assert state is not None
        assert "do-not-store" not in state["error_message"]
        assert "also-secret" not in state["error_message"]
        assert state["error_message"].count("[REDACTED]") == 2
    finally:
        cache.close()
