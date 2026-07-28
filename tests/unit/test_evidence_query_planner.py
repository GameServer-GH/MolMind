from __future__ import annotations

from datetime import timedelta

from services.evidence_gateway import (
    EvidenceQueryCache,
    plan_provider_queries,
    resolve_identity,
)

INCHIKEY = "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
STANDARDIZED_INCHIKEY = "ATUOYWHBWRKTHZ-UHFFFAOYSA-N"


def test_planner_prefers_local_hit_and_plans_missing_sources(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        cache.record(
            source_id="epa_ctx",
            entity_key=INCHIKEY,
            endpoint="identity_lookup",
            status="hit",
            ttl=timedelta(days=1),
            payload={"hits": [{"evidence_id": "epa:cached"}]},
        )
        tasks = plan_provider_queries(
            cache,
            [
                {
                    "molecule_id": "M1",
                    "original_inchikey": INCHIKEY,
                    "standardized_inchikey": STANDARDIZED_INCHIKEY,
                    "cas": "50-00-0",
                    "standardization_steps": ["fragment_parent"],
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
        assert tasks[1].lookup_value == INCHIKEY
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


def test_identity_resolver_prioritizes_original_smiles_derived_key() -> None:
    resolution = resolve_identity(
        molecule_id="M1",
        standardized_inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
        original_smiles="CCO",
    )
    assert resolution.status == "hit"
    assert resolution.lookup_field == "original_inchikey"
    assert resolution.lookup_value == "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
    assert resolution.match_type == "inchikey_derived_from_original_smiles"


def test_invalid_identifiers_are_audit_missing_not_lookup_candidates() -> None:
    resolution = resolve_identity(
        molecule_id="M1",
        original_inchikey="NOT-A-VALID-KEY",
        cas="123-45-6",
    )
    assert resolution.status == "audit_missing"
    assert resolution.is_resolved is False
    assert "original_inchikey_invalid" in resolution.conflicts
    assert "cas_invalid" in resolution.conflicts


def test_cross_standardization_key_change_requires_audited_steps() -> None:
    unexplained = resolve_identity(
        molecule_id="M1",
        original_inchikey=INCHIKEY,
        standardized_inchikey="OKKJLVBELUTLKV-UHFFFAOYSA-N",
    )
    explained = resolve_identity(
        molecule_id="M1",
        original_inchikey=INCHIKEY,
        standardized_inchikey="OKKJLVBELUTLKV-UHFFFAOYSA-N",
        standardization_steps=["fragment_parent"],
    )
    assert unexplained.status == "identity_review_required"
    assert "unexplained_standardization_identity_change" in unexplained.conflicts
    assert explained.status == "hit"
    assert "standardization_changed_inchikey_with_audited_steps" in explained.notes


def test_explicit_key_must_match_corresponding_smiles_identity() -> None:
    original_conflict = resolve_identity(
        molecule_id="M1",
        original_inchikey=INCHIKEY,
        original_smiles="CO",
    )
    standardized_conflict = resolve_identity(
        molecule_id="M1",
        standardized_inchikey=INCHIKEY,
        smiles="CO",
    )
    assert original_conflict.status == "identity_review_required"
    assert "original_smiles_inchikey_conflict" in original_conflict.conflicts
    assert standardized_conflict.status == "identity_review_required"
    assert "standardized_smiles_inchikey_conflict" in standardized_conflict.conflicts


def test_identity_conflict_blocks_every_provider_lookup(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        resolution = resolve_identity(
            molecule_id="M1",
            standardized_inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
            smiles="CCC",
        )
        tasks = plan_provider_queries(
            cache,
            [resolution],
            {"chembl": {"identity_order": ["standardized_inchikey"]}},
            online=True,
        )
        assert resolution.status == "identity_review_required"
        assert len(tasks) == 1
        assert tasks[0].action == "offline_missing"
        assert tasks[0].decision.status == "identity_review_required"
    finally:
        cache.close()


def test_planner_merges_query_types_that_share_provider_endpoint(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        tasks = plan_provider_queries(
            cache,
            [{"molecule_id": "M1", "original_inchikey": INCHIKEY}],
            {
                "chembl": {
                    "identity_order": ["original_inchikey"],
                    "query_types": {
                        "lipid": {"endpoint": "molecule_activity"},
                        "tox": {"endpoint": "molecule_activity"},
                        "pathway": {"endpoint": "molecule_activity"},
                    },
                },
                "pubchem": {"identity_order": ["original_inchikey"]},
            },
            providers=["chembl"],
            query_types=["tox", "lipid"],
            online=True,
        )
        assert len(tasks) == 1
        assert tasks[0].provider_id == "chembl"
        assert tasks[0].endpoint == "molecule_activity"
        assert tasks[0].query_type == "tox,lipid"
        assert tasks[0].entity_key == tasks[0].lookup_value == INCHIKEY
    finally:
        cache.close()


def test_local_only_provider_never_plans_remote_lookup(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        tasks = plan_provider_queries(
            cache,
            [{"molecule_id": "M1", "original_inchikey": INCHIKEY}],
            {
                "bindingdb": {
                    "identity_order": ["original_inchikey"],
                    "live_supported": False,
                    "query_types": {"pathway": {"endpoint": "target_ligand"}},
                }
            },
            providers=["bindingdb"],
            online=True,
        )
        assert len(tasks) == 1
        assert tasks[0].action == "offline_missing"
        assert tasks[0].decision.status == "not_queried"
        assert "local-only adapter unavailable" in tasks[0].decision.reason
    finally:
        cache.close()


def test_unsupported_explicit_query_type_gets_auditable_task(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        tasks = plan_provider_queries(
            cache,
            [{"molecule_id": "M1", "original_inchikey": INCHIKEY}],
            {
                "pubchem": {
                    "identity_order": ["original_inchikey"],
                    "query_types": {"tox": {"endpoint": "compound_bundle"}},
                }
            },
            providers=["pubchem"],
            query_types=["identity"],
            online=True,
        )
        assert len(tasks) == 1
        assert tasks[0].query_type == "identity"
        assert tasks[0].action == "offline_missing"
        assert tasks[0].decision.status == "not_queried"
        assert tasks[0].decision.reason == "unsupported_query_type:identity"
    finally:
        cache.close()


def test_disabled_provider_is_reported_instead_of_silently_dropped(tmp_path) -> None:
    cache = EvidenceQueryCache(tmp_path / "state.sqlite")
    try:
        tasks = plan_provider_queries(
            cache,
            [{"molecule_id": "M1", "original_inchikey": INCHIKEY}],
            {
                "chembl": {
                    "enabled": False,
                    "identity_order": ["original_inchikey"],
                    "query_types": {"lipid": {"endpoint": "molecule_activity"}},
                }
            },
            providers=["chembl"],
            online=True,
        )
        assert len(tasks) == 1
        assert tasks[0].action == "offline_missing"
        assert tasks[0].decision.status == "not_queried"
        assert tasks[0].decision.reason == "provider disabled by evidence policy"
    finally:
        cache.close()
