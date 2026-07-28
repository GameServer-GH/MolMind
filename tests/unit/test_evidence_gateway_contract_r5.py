from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict

from packages.models import EvidenceHit
from plugins.molmind_core.scientific.evidence_facade.bake import (
    promote_evidence_cache,
)
from plugins.molmind_core.scientific.evidence_gateway.cache import EvidenceQueryCache
from plugins.molmind_core.scientific.evidence_gateway.planner import (
    plan_provider_queries,
)
from plugins.molmind_core.scientific.evidence_gateway.retriever import EvidenceRetriever

KEY = "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"


def _provider(*types: str) -> dict:
    return {
        "identity_order": ["original_inchikey"],
        "query_types": {name: {"endpoint": "bundle"} for name in types},
        "adapter_version": "contract-test-v1",
        "transport_api_version": "test-v1",
        "timeout_sec": 1,
    }


def test_query_contract_hash_separates_query_types_and_invalidates_cache(tmp_path):
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        cfg = {"providers": {"p": _provider("lipid", "tox")}}
        first = plan_provider_queries(
            cache,
            [{"molecule_id": "M1", "original_inchikey": KEY}],
            cfg["providers"],
            online=True,
            query_types=["lipid"],
        )[0]
        second = plan_provider_queries(
            cache,
            [{"molecule_id": "M1", "original_inchikey": KEY}],
            cfg["providers"],
            online=True,
            query_types=["tox"],
        )[0]
        assert first.query_contract_hash != second.query_contract_hash
        cache.record(
            source_id="p",
            entity_key=KEY,
            endpoint="bundle",
            status="hit",
            payload={"hits": []},
            adapter_version="contract-test-v1",
            query_type="lipid",
            query_contract_hash=first.query_contract_hash,
        )
        assert cache.decide(
            source_id="p",
            entity_key=KEY,
            endpoint="bundle",
            online=False,
            expected_query_contract_hash=second.query_contract_hash,
        ).status == "not_queried"
    finally:
        cache.close()


def test_total_deadline_returns_query_failed_and_stops_new_work(tmp_path):
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    calls: list[str] = []

    def slow(task):
        calls.append(task.molecule_id)
        time.sleep(0.2)
        return []

    retriever = EvidenceRetriever(
        cache,
        {"providers": {"p": _provider("lipid")}},
        {"p": slow},
    )
    try:
        result = retriever.query(
            [
                {"molecule_id": "M1", "original_inchikey": KEY},
                {"molecule_id": "M2", "original_inchikey": KEY},
            ],
            providers=["p"],
            allow_live=True,
            total_timeout_sec=0.03,
        )
        assert result.timed_out is True
        assert any(
            hit.query_status == "query_failed"
            for hits in result.audits_by_molecule.values()
            for hit in hits
        )
        assert result.events[-1]["type"] == "query_summary"
    finally:
        cache.close()


def test_cancelled_query_has_no_late_event_delivery(tmp_path):
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    cancel = threading.Event()
    events: list[dict] = []

    def adapter(task):
        time.sleep(0.05)
        return []

    cancel.set()
    retriever = EvidenceRetriever(
        cache,
        {"providers": {"p": _provider("lipid")}},
        {"p": adapter},
    )
    try:
        result = retriever.query(
            [{"molecule_id": "M1", "original_inchikey": KEY}],
            providers=["p"],
            allow_live=True,
            cancel_event=cancel,
            event_sink=events.append,
        )
        before = len(events)
        time.sleep(0.08)
        assert len(events) == before
        assert result.cancelled is True
    finally:
        cache.close()


def test_legacy_cache_schema_migrates_without_immortal_fresh_rows(tmp_path):
    path = tmp_path / "legacy.sqlite"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE source_query (
          source_id TEXT NOT NULL, entity_key TEXT NOT NULL, endpoint TEXT NOT NULL,
          status TEXT NOT NULL, retrieved_at TEXT, expires_at TEXT,
          next_retry_at TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
          payload_path TEXT, payload_json TEXT, payload_sha256 TEXT,
          source_version TEXT, error_type TEXT, error_message TEXT,
          lookup_field TEXT, lookup_value TEXT, match_type TEXT,
          endpoint_url TEXT, adapter_version TEXT, query_type TEXT,
          PRIMARY KEY (source_id, entity_key, endpoint)
        );
        """
    )
    db.execute(
        "INSERT INTO source_query(source_id,entity_key,endpoint,status) VALUES (?,?,?,?)",
        ("p", KEY, "bundle", "hit"),
    )
    db.commit()
    db.close()
    cache = EvidenceQueryCache(path)
    try:
        assert "query_contract_hash" in {
            row["name"]
            for row in cache.db.execute("pragma table_info(source_query)")
        }
        assert cache.decide(
            source_id="p",
            entity_key=KEY,
            endpoint="bundle",
            online=False,
            expected_query_contract_hash="new-contract",
        ).status == "not_queried"
    finally:
        cache.close()


def test_cache_promote_rejects_failed_rows_and_publishes_atomically(tmp_path):
    cache_path = tmp_path / "state.sqlite"
    cache = EvidenceQueryCache(cache_path)
    hit = EvidenceHit(
        adapter_id="p",
        provider_id="p",
        query_type="lipid",
        score=0.7,
        confidence=0.8,
        evidence_id="p:1",
        evidence_role="task_evidence",
        query_status="hit",
        lookup_field="original_inchikey",
        lookup_value=KEY,
        endpoint="bundle",
    )
    try:
        cache.record(
            source_id="p",
            entity_key=KEY,
            endpoint="bundle",
            status="hit",
            payload={"hits": [asdict(hit)]},
            adapter_version="contract-test-v1",
            query_contract_hash="contract",
        )
        cache.record(
            source_id="p",
            entity_key="BAD",
            endpoint="bundle",
            status="query_failed",
            query_contract_hash="contract",
        )
    finally:
        cache.close()
    output = tmp_path / "snapshot.jsonl"
    stats = promote_evidence_cache(
        cache_path=cache_path,
        output_path=output,
    )
    assert stats.rows == 1
    assert stats.rejected == 0  # failed state has no payload and is skipped
    assert output.exists()
    assert output.with_suffix(".jsonl.manifest.json").exists()
    row = json.loads(output.read_text().splitlines()[0])
    assert row["query_status"] == "hit"
