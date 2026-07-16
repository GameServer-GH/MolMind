"""Public assay-grain QC and EvidenceFacade wiring."""

from __future__ import annotations

import json
from pathlib import Path

from packages.chem_core import compute_descriptors, morgan_fp
from packages.models import MoleculeRecord
from rdkit import Chem

from services.evidence_facade import EvidenceFacade
from services.pipeline.config_loader import load_config
from services.public_data.assay_index import load_public_assay_index, row_to_evidence_hit
from services.public_data.qc import filter_records, qc_pubchem_row, run_assay_grain_qc


ROOT = Path(__file__).resolve().parents[2]


def test_pubchem_unspecified_is_excluded_from_endpoint_qc() -> None:
    ok, reason, _ = qc_pubchem_row(
        {
            "compound_id": "CID:1",
            "standardized_smiles": "CCO",
            "inchikey": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
            "assay_id": "AID:1",
            "direction": "Unspecified",
            "endpoint": "IC50",
            "value": 1.0,
        }
    )
    assert ok is False
    assert reason.startswith("outcome_excluded")


def test_pubchem_active_numeric_passes_as_annotation_only() -> None:
    ok, reason, row = qc_pubchem_row(
        {
            "compound_id": "CID:1",
            "standardized_smiles": "CCO",
            "inchikey": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
            "assay_id": "AID:1",
            "direction": "Active",
            "endpoint": "IC50",
            "value": 2.5,
            "unit": "uM",
        }
    )
    assert ok is True
    assert reason == "pass"
    assert row["evidence_role"] == "annotation_only"
    assert row["molmind_direction"] == "unknown"
    assert row["eligible_for_endpoint_training"] is True
    hit = row_to_evidence_hit(row)
    assert hit.score == 0.0
    assert hit.confidence == 0.0
    assert hit.query_type == "annotation"


def test_chembl_adverse_becomes_tox_task_evidence() -> None:
    rows = [
        {
            "compound_id": "CHEMBL1",
            "standardized_smiles": "CCO",
            "assay_id": "CHEMBL999",
            "classification": "adverse_phenotype",
            "direction": "risk",
            "endpoint": "Activity",
            "source_id": "chembl_bioactivity",
        }
    ]
    kept, reasons = filter_records(rows, source="chembl_bioactivity")
    assert reasons.get("pass") == 1
    assert kept[0]["evidence_type"] == "endpoint_evidence"
    hit = row_to_evidence_hit(kept[0])
    assert hit.query_type == "tox"
    assert hit.direction == "risk"
    assert hit.score > 0


def test_run_assay_grain_qc_writes_manifest(tmp_path: Path, monkeypatch) -> None:
    from services.public_data import qc as qc_mod

    pub = tmp_path / "pubchem" / "records.jsonl"
    chem = tmp_path / "chembl" / "records.jsonl"
    pub.parent.mkdir(parents=True)
    chem.parent.mkdir(parents=True)
    pub.write_text(
        json.dumps(
            {
                "compound_id": "CID:1",
                "standardized_smiles": "CCO",
                "inchikey": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
                "assay_id": "AID:1",
                "direction": "Active",
                "endpoint": "IC50",
                "value": 1.0,
                "unit": "uM",
            }
        )
        + "\n"
        + json.dumps(
            {
                "compound_id": "CID:2",
                "standardized_smiles": "CCC",
                "inchikey": "ATUOYWHBWRKTHZ-UHFFFAOYSA-N",
                "assay_id": "AID:2",
                "direction": "Unspecified",
                "endpoint": "depositor_assay_activity",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    chem.write_text(
        json.dumps(
            {
                "compound_id": "CHEMBL1",
                "standardized_smiles": "CCO",
                "assay_id": "CHEMBL9",
                "classification": "annotation",
                "endpoint": "Activity",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(qc_mod, "MANIFESTS", tmp_path / "manifests")
    monkeypatch.setattr(qc_mod, "ROOT", tmp_path)
    report = run_assay_grain_qc(
        pubchem_path=pub,
        chembl_path=chem,
        bindingdb_path=tmp_path / "missing.jsonl",
        toxcast_path=tmp_path / "missing_tox.jsonl",
    )
    assert report["sources"]["pubchem_bioassay"]["qc_pass_rows"] == 1
    assert report["sources"]["pubchem_bioassay"]["input_rows"] == 2
    assert report["sources"]["chembl_bioactivity"]["qc_pass_rows"] == 0
    assert report["sources"]["bindingdb"]["status"] == "audit_missing"
    assert report["sources"]["epa_toxcast_tox21"]["status"] == "audit_missing"
    assert (tmp_path / "manifests" / "assay_grain_qc.json").is_file()


def test_facade_merges_public_qc_by_inchikey(tmp_path: Path) -> None:
    qc_path = tmp_path / "qc.jsonl"
    # ethanol InChIKey
    inchikey = "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
    qc_path.write_text(
        json.dumps(
            {
                "compound_id": "CID:702",
                "standardized_smiles": "CCO",
                "inchikey": inchikey,
                "assay_id": "AID:1",
                "endpoint": "IC50",
                "value": 1.0,
                "unit": "uM",
                "qc_source": "pubchem_bioassay",
                "qc_tier": "numeric_active",
                "molmind_direction": "unknown",
                "evidence_role": "annotation_only",
                "evidence_type": "identity_annotation",
                "eligible_for_endpoint_training": True,
                "source_url": "https://example.test",
                "retrieved_at": "2026-07-15T00:00:00+00:00",
                "license": "test",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    cfg = load_config(mode="offline")
    cfg.raw["evidence"]["public_assay_grain"] = {
        "enabled": True,
        "allow_chembl_phenotype_scores": True,
        "qc_paths": [str(qc_path)],
    }
    # reload path resolution uses list of Path from config — EvidenceFacade reads cfg.evidence
    cfg.evidence["public_assay_grain"] = cfg.raw["evidence"]["public_assay_grain"]
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path / "empty_snap")
    facade._public_assay_index = load_public_assay_index([qc_path])
    bundle = facade.query(inchikey=inchikey, cas=None, smiles="CCO", allow_live=False)
    assert any(hit.adapter_id.startswith("public_pubchem") for hit in bundle.annotation)
    assert bundle.conf_e == 0.0
