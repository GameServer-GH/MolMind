"""Public HepG2-FFA resources remain non-scoring and cannot fake a task model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.evidence_facade.hepg2_ffa_resources import (
    HepG2FFAResourceError,
    evaluate_dual_endpoint_training_record,
    load_hepg2_ffa_resource_registry,
    resource_registry_runtime_payload,
)


def test_registry_normalizes_accessions_and_keeps_context_non_scoring() -> None:
    registry = load_hepg2_ffa_resource_registry()
    assert registry.ranking_effect == "none"
    assert registry.canonicalize_accession("PXD012066-2") == "PXD012066"
    assert registry.canonicalize_accession("PXD007902-2") == "PXD007902"
    assert len(registry.resources) == 6
    assert registry.mechanistic_context_count == 5
    assert registry.assay_qc_count == 1
    assert registry.candidate_dual_endpoint_resource_count == 0
    assert not any(item.training_eligible for item in registry.resources)
    assert all(item.ineligibility_reasons for item in registry.resources)


def test_ssbd_summary_is_frozen_but_not_a_candidate_endpoint() -> None:
    registry = load_hepg2_ffa_resource_registry()
    ssbd = next(item for item in registry.resources if item.canonical_accession == "SSBD:dataset-12051")
    assert ssbd.role == "assay_qc"
    assert ssbd.raw["archive_sha256"] == "cd14642952f1a0c264619e8492f030d2b3018b8ada6bce9ac481b5f5349d6ed9"
    assert ssbd.assay_context["spectra_columns"] == 173
    assert sum(ssbd.assay_context["spectra_replicates_by_condition"].values()) == 173
    assert not ssbd.training_eligible


def test_training_gate_requires_same_condition_paired_endpoints() -> None:
    incomplete = evaluate_dual_endpoint_training_record(
        {
            "compound_id": "X",
            "standardized_smiles": "CCO",
            "lipid_response": 0.5,
            "cell_viability_response": 0.9,
            "lipid_condition_id": "batch-a-dose-1",
            "viability_condition_id": "batch-b-dose-1",
        }
    )
    assert not incomplete.eligible
    assert "lipid_viability_conditions_not_identical" in incomplete.reasons

    complete = {
        "compound_id": "X",
        "standardized_smiles": "CCO",
        "dose": 10,
        "dose_unit": "uM",
        "treatment_time_hours": 24,
        "ffa_composition": "oleate:palmitate=2:1",
        "lipid_response": 0.5,
        "cell_viability_response": 0.9,
        "batch_id": "batch-a",
        "vehicle_control_id": "vehicle-a",
        "lipid_condition_id": "batch-a-dose-1",
        "viability_condition_id": "batch-a-dose-1",
        "lipid_assay": "Nile Red",
        "viability_assay": "CellTiter-Glo",
        "source_id": "doi:test",
    }
    decision = evaluate_dual_endpoint_training_record(complete)
    assert decision.eligible
    assert not decision.reasons


def test_runtime_payload_preserves_unavailable_scientific_status() -> None:
    payload = resource_registry_runtime_payload()
    assert payload["ranking_effect"] == "none"
    assert payload["dual_endpoint_model_available"] is False
    assert payload["resource_counts"]["candidate_dual_endpoint_training_eligible"] == 0
    assert payload["scientific_validation_status"] == "no_validated_independent_dual_endpoint_benchmark"


def test_registry_rejects_context_that_claims_ranking_effect(tmp_path: Path) -> None:
    original = load_hepg2_ffa_resource_registry()
    payload = json.loads(
        (Path(__file__).resolve().parents[2] / "data/evidence_snapshot/v2/hepg2_ffa_resources_v1.json").read_text()
    )
    payload["ranking_effect"] = "positive"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert original.ranking_effect == "none"
    with pytest.raises(HepG2FFAResourceError, match="ranking_effect=none"):
        load_hepg2_ffa_resource_registry(path)
