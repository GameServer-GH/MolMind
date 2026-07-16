from __future__ import annotations

from pathlib import Path

import pytest

from scripts.validate_public_data_registry import PublicRegistryError, load_registry


ROOT = Path(__file__).resolve().parents[2]


def test_public_registry_is_quality_first_and_non_false_negative() -> None:
    payload = load_registry()
    assert payload["project_policy"]["quality_first"] is True
    assert payload["project_policy"]["keep_training_and_inference_in_same_project"] is True
    assert payload["project_policy"]["negative_search_is_negative_label"] is False
    assert payload["project_policy"]["import_wave_order"] == [
        "wave_1_activity",
        "wave_2_toxicology",
        "wave_3_multiomics",
    ]
    assert len(payload["sources"]) >= 8
    wave_rank = {
        "wave_1_activity": 1,
        "wave_2_toxicology": 2,
        "wave_3_multiomics": 3,
    }
    last = 0
    for source in payload["sources"]:
        assert source["missing_semantics"] == "audit_missing"
        assert source["import_wave"] in wave_rank
        assert wave_rank[source["import_wave"]] >= last
        last = wave_rank[source["import_wave"]]
    assert any(s["source_id"] == "chembl_bioactivity" for s in payload["sources"])
    assert any(s["source_id"] == "epa_toxcast_tox21" for s in payload["sources"])
    assert any(s["source_id"] == "geo_ffa_and_drug_signatures" for s in payload["sources"])


def test_mechanism_sources_cannot_rank(tmp_path: Path) -> None:
    # Mutate only the first mechanism ranking policy in a parsed copy.
    payload = load_registry()
    geo = next(item for item in payload["sources"] if item["source_id"] == "geo_ffa_and_drug_signatures")
    geo["ranking_effect"] = "candidate_task_evidence_only"
    bad = tmp_path / "bad.yaml"
    import yaml
    bad.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    with pytest.raises(PublicRegistryError, match="mechanism source cannot directly affect ranking"):
        load_registry(bad)
