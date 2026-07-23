from __future__ import annotations

from datetime import timedelta

from services.evidence_gateway import EvidenceQueryCache, plan_provider_queries


def test_planner_prefers_local_hit_and_plans_missing_sources(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        cache.record(
            source_id="epa_ctx",
            entity_key="IK1",
            endpoint="identity_lookup",
            status="hit",
            ttl=timedelta(days=1),
            payload_path="objects/epa.json",
        )
        tasks = plan_provider_queries(
            cache,
            [
                {
                    "molecule_id": "M1",
                    "original_inchikey": "IK1",
                    "standardized_inchikey": "SIK1",
                    "cas": "1-11-1",
                }
            ],
            {
                "epa_ctx": {"identity_order": ["original_inchikey", "cas"]},
                "chembl": {"identity_order": ["original_inchikey", "standardized_inchikey"]},
            },
            online=True,
        )
        assert [(task.provider_id, task.action) for task in tasks] == [
            ("epa_ctx", "local_hit"),
            ("chembl", "query_remote"),
        ]
        assert tasks[1].lookup_field == "original_inchikey"
        assert tasks[1].lookup_value == "IK1"
    finally:
        cache.close()


def test_planner_uses_cas_as_cross_sdf_fallback_key(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        tasks = plan_provider_queries(
            cache,
            [{"molecule_id": "SDF:7", "cas": "50-00-0"}],
            {"epa_ctx": {"identity_order": ["original_inchikey", "cas"]}},
            online=True,
        )
        assert tasks[0].entity_key == "50-00-0"
        assert tasks[0].lookup_field == "cas"
        assert tasks[0].action == "query_remote"
    finally:
        cache.close()
