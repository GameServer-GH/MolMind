"""EPA CTX staged reporting/scoring and PubChem semantic guardrails."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from packages.chem_core import compute_descriptors, morgan_fp
from packages.goldset import load_goldset
from packages.models import MoleculeRecord
from services.evidence_facade import EvidenceFacade
from services.evidence_facade.epa_index import EPAContextIndex
from services.evidence_facade.facade import _normalize_snapshot_row
from services.pipeline.config_loader import load_config
from services.ranker.ranker import score_molecule
from services.scorer_tox import score_tox


def _record(mid: str = "T-EPA", smiles: str = "CCOc1ccc(CC)cc1") -> MoleculeRecord:
    desc = compute_descriptors(smiles)
    assert desc is not None
    from rdkit import Chem

    mol = Chem.MolFromSmiles(smiles)
    assert mol is not None
    return MoleculeRecord(
        molecule_id=mid,
        smiles=smiles,
        inchikey=Chem.MolToInchiKey(mol) or "",
        cas="111-11-1",
        mw=float(desc["mw"]),
        logp=float(desc["logp"]),
        hbd=int(desc["hbd"]),
        hba=int(desc["hba"]),
        tpsa=float(desc["tpsa"]),
        rotatable_bonds=int(desc["rotatable_bonds"]),
        aromatic_rings=int(desc["aromatic_rings"]),
        fp_bits=morgan_fp(mol),
    )


def _cfg(tmp_path: Path, stage: int, *, strong: bool = True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    mapping = tmp_path / "candidate_mapping.jsonl"
    summary = tmp_path / "candidate_risk_summary.jsonl"
    record = _record()
    mapping.write_text(
        json.dumps(
            {
                "molecule_id": "T-EPA",
                "dtxsid": "DTXSID9001",
                "casrn": "111-11-1",
                "mapping_status": "exact_identifier_match",
                "mapping_basis": "original_inchikey",
                "mapping_value": record.inchikey,
                "original_inchikey": record.inchikey,
                "standardized_inchikey": record.inchikey,
                "standardized_smiles": record.smiles,
                "retrieved_at": "2026-07-23T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    if strong:
        summary_row = {
            "molecule_id": "T-EPA",
            "dtxsid": "DTXSID9001",
            "status": "returned",
            "mapping_status": "exact_identifier_match",
            "bioactivity_record_count": 12,
            "active_hit_count_hitc_gte_0_9": 2,
            "nhit": 5,
            "cytotoxLowerUm": 1.0,
            "cytotoxMedianUm": 3.0,
            "active_aeids": [101, 102],
            "toxval_record_count": 0,
            "toxref_summary_record_count": 0,
            "interpretation": "cytotox_strong_risk",
            "retrieved_at": "2026-07-23T00:00:00+00:00",
        }
    else:
        summary_row = {
            "molecule_id": "T-EPA",
            "dtxsid": "DTXSID9001",
            "status": "returned",
            "mapping_status": "exact_identifier_match",
            "bioactivity_record_count": 12,
            "active_hit_count_hitc_gte_0_9": 6,
            "nhit": 0,
            "cytotoxLowerUm": 1000.0,
            "cytotoxMedianUm": 1000.0,
            "active_aeids": [101],
            "toxval_record_count": 0,
            "toxref_summary_record_count": 0,
            "interpretation": "bioactivity_annotation",
            "retrieved_at": "2026-07-23T00:00:00+00:00",
        }
    summary.write_text(json.dumps(summary_row) + "\n", encoding="utf-8")
    cfg = load_config(mode="offline", epa_stage=stage)
    cfg.raw["evidence"]["epa_ctx"] = {
        "enabled": True,
        "integration_stage": stage,
        "mapping_paths": [str(mapping)],
        "risk_summary_paths": [str(summary)],
        "assay_qc_paths": [],
        "require_exact_identity_for_stage2": True,
        "share_risk_across_standardized_smiles": True,
        "cytotox_screening_um": 10.0,
        "max_risk_score": 0.40,
        "risk_confidence": 0.50,
    }
    return cfg


def test_epa_stage1_is_report_only(tmp_path: Path) -> None:
    record = _record()
    cfg = _cfg(tmp_path, 1)
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path / "empty")
    bundle = facade.query(
        inchikey=record.inchikey,
        cas=record.cas,
        smiles=record.smiles,
        allow_live=False,
    )

    assert bundle.epa_audit["status"] == "cytotox_strong_risk"
    assert bundle.epa_audit["risk_applied"] is False
    assert bundle.tox == []
    assert bundle.annotation[0].adapter_id == "epa_ctx_v1"
    assert bundle.annotation[0].score == 0.0


def test_epa_stage2_adds_bounded_risk_only(tmp_path: Path) -> None:
    record = _record()
    cfg = _cfg(tmp_path, 2)
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path / "empty")
    bundle = facade.query(
        inchikey=record.inchikey,
        cas=record.cas,
        smiles=record.smiles,
        allow_live=False,
    )
    no_epa = EvidenceFacade(_cfg(tmp_path / "no-epa", 0), snapshot_dir=tmp_path / "no-epa-empty")
    baseline = no_epa.query(
        inchikey=record.inchikey,
        cas=record.cas,
        smiles=record.smiles,
        allow_live=False,
    )
    gold = load_goldset()
    scored_epa, *_ = score_tox(record, cfg, gold, bundle)
    scored_base, *_ = score_tox(record, cfg, gold, baseline)

    assert bundle.epa_audit["risk_applied"] is True
    assert bundle.epa_audit["cytotox_risk_tier"] == "strong_risk"
    assert len(bundle.tox) == 1
    assert bundle.tox[0].adapter_id == "epa_ctx_tox_v1"
    assert bundle.tox[0].score == pytest.approx(0.40)
    assert scored_epa > scored_base


def test_epa_stage2_active_without_nhit_is_annotation_only(tmp_path: Path) -> None:
    record = _record()
    cfg = _cfg(tmp_path, 2, strong=False)
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path / "empty")
    bundle = facade.query(
        inchikey=record.inchikey,
        cas=record.cas,
        smiles=record.smiles,
        allow_live=False,
    )
    assert bundle.tox == []
    assert bundle.epa_audit["risk_applied"] is False
    assert bundle.epa_audit["cytotox_risk_tier"] == "bioactivity_annotation"
    assert bundle.epa_audit["active_hit_count"] == 6
    assert any(hit.adapter_id == "epa_ctx_v1" for hit in bundle.annotation)


def test_epa_stage2_cas_identity_audit_does_not_cancel_eligibility(
    tmp_path: Path,
) -> None:
    record = _record()
    cfg = _cfg(tmp_path, 2)
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path / "empty")
    bundle = facade.query(
        inchikey="OTHER-AAAAA-BBBBBBBBBB-N",
        cas=record.cas,
        smiles=record.smiles,
        allow_live=False,
    )
    assert bundle.tox == []
    assert bundle.epa_audit["matched_identity_type"] == "cas"
    assert bundle.epa_audit["query_status"] == "identity_review_required"
    assert not any(
        hit.query_status == "identity_review_required" for hit in bundle.all_hits()
    )
    assert bundle.has_identity_review_required is False

    gold = load_goldset()
    # Provide lipid task evidence so eligibility is not blocked for other reasons.
    from packages.models import EvidenceHit

    bundle.lipid.append(
        EvidenceHit(
            adapter_id="chembl_lipid_v1",
            query_type="lipid",
            score=0.8,
            confidence=0.8,
            evidence_id="chembl:CAS-AUDIT:lipid",
            evidence_role="task_evidence",
            direction="supports",
            query_status="exact_hit",
            evidence_type="endpoint_evidence",
        )
    )
    scored = score_molecule(record, cfg, gold, bundle)
    assert scored.scientific_status != "identity_review_required"
    assert "identity_review_required" not in scored.eligibility_reasons


def test_epa_stage2_empty_cas_mapping_still_audits_without_gating(
    tmp_path: Path,
) -> None:
    record = _record()
    cfg = _cfg(tmp_path, 2)
    mapping_path = Path(cfg.evidence["epa_ctx"]["mapping_paths"][0])
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    mapping.update(
        {
            "mapping_status": "identifier_match_requires_structure_audit",
            "mapping_basis": "cas",
        }
    )
    mapping_path.write_text(json.dumps(mapping) + "\n", encoding="utf-8")
    summary_path = Path(cfg.evidence["epa_ctx"]["risk_summary_paths"][0])
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.update(
        {
            "status": "verified_empty",
            "bioactivity_record_count": 0,
            "active_hit_count_hitc_gte_0_9": 0,
            "nhit": 0,
            "active_aeids": [],
        }
    )
    summary_path.write_text(json.dumps(summary) + "\n", encoding="utf-8")

    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path / "empty")
    bundle = facade.query(
        inchikey=record.inchikey,
        cas=record.cas,
        smiles=record.smiles,
        allow_live=False,
    )

    assert bundle.epa_audit["status"] == "verified_empty"
    assert bundle.epa_audit["query_status"] == "identity_review_required"
    assert bundle.epa_audit["risk_applied"] is False
    assert bundle.tox == []
    assert bundle.has_identity_review_required is False


def test_epa_shares_strong_risk_across_identical_standardized_smiles(
    tmp_path: Path,
) -> None:
    smiles = "CCOc1ccc(CC)cc1"
    parent_key = "PARENTAAAA-BBBBBBBBBB-N"
    salt_key = "SALTAAAAAA-BBBBBBBBBB-M"
    mapping = tmp_path / "candidate_mapping.jsonl"
    summary = tmp_path / "candidate_risk_summary.jsonl"
    mapping.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "molecule_id": "T-PARENT",
                        "dtxsid": "DTXSID-PARENT",
                        "mapping_status": "exact_identifier_match",
                        "mapping_basis": "original_inchikey",
                        "mapping_value": parent_key,
                        "original_inchikey": parent_key,
                        "standardized_inchikey": parent_key,
                        "standardized_smiles": smiles,
                        "preferred_name": "Parent",
                    }
                ),
                json.dumps(
                    {
                        "molecule_id": "T-SALT",
                        "dtxsid": "DTXSID-SALT",
                        "mapping_status": "exact_identifier_match",
                        "mapping_basis": "original_inchikey",
                        "mapping_value": salt_key,
                        "original_inchikey": salt_key,
                        "standardized_inchikey": salt_key,
                        "standardized_smiles": smiles,
                        "preferred_name": "Salt",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    summary.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "dtxsid": "DTXSID-PARENT",
                        "status": "verified_empty",
                        "active_hit_count_hitc_gte_0_9": 0,
                        "nhit": 0,
                    }
                ),
                json.dumps(
                    {
                        "dtxsid": "DTXSID-SALT",
                        "status": "returned",
                        "active_hit_count_hitc_gte_0_9": 111,
                        "nhit": 26,
                        "cytotoxLowerUm": 0.08,
                        "cytotoxMedianUm": 0.27,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    cfg = load_config(mode="offline", epa_stage=2)
    cfg.raw["evidence"]["epa_ctx"] = {
        "enabled": True,
        "integration_stage": 2,
        "mapping_paths": [str(mapping)],
        "risk_summary_paths": [str(summary)],
        "assay_qc_paths": [],
        "share_risk_across_standardized_smiles": True,
        "cytotox_screening_um": 10.0,
        "max_risk_score": 0.40,
        "risk_confidence": 0.50,
    }
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path / "empty")
    bundle = facade.query(
        inchikey=parent_key,
        cas=None,
        smiles=smiles,
        allow_live=False,
    )
    assert bundle.epa_audit["risk_applied"] is True
    assert bundle.epa_audit["risk_inherited_from_dtxsid"] == "DTXSID-SALT"
    assert bundle.epa_audit["cytotox_risk_tier"] == "strong_risk"
    assert bundle.tox[0].score == pytest.approx(0.40)


def test_pubchem_dili_negative_is_not_liver_risk(tmp_path: Path) -> None:
    cfg = load_config(mode="offline")
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path / "empty")
    cid_response = MagicMock(status_code=200, content=b'{"cid": 5733}')
    cid_response.json.return_value = {"IdentifierList": {"CID": [5733]}}
    view_response = MagicMock(status_code=200, content=b'{"dili":"negative"}')
    view_response.json.return_value = {
        "TOCHeading": "Toxicity",
        "Section": [
            {
                "TOCHeading": "Drug Induced Liver Injury",
                "Section": [
                    {
                        "TOCHeading": "DILIst Classification",
                        "Information": [
                            {"Value": {"StringWithMarkup": [{"String": "DILI Negative"}]}}
                        ],
                    }
                ],
            }
        ],
    }
    client = MagicMock()
    client.get.side_effect = [cid_response, view_response]

    hits = facade._pubchem_tox(client, "DILI-NEGATIVE-KEY")

    assert len(hits) == 1
    assert hits[0].query_type == "annotation"
    assert hits[0].score == 0.0
    assert hits[0].payload["dili_classification"] == "negative"


def test_legacy_pubchem_dili_negative_snapshot_is_migrated() -> None:
    migrated = _normalize_snapshot_row(
        {
            "adapter_id": "pubchem_tox_v1",
            "query_type": "tox",
            "score": 0.35,
            "confidence": 0.55,
            "evidence_id": "pubchem:5733:ghs",
            "evidence_role": "task_evidence",
            "direction": "risk",
            "payload": {
                "cid": 5733,
                "flags": ["liver"],
                "matched_nodes": [
                    {
                        "path": (
                            "Toxicity > Toxicological Information > "
                            "Drug Induced Liver Injury > DILIst Classification"
                        ),
                        "value": "DILI Negative",
                    }
                ],
            },
        }
    )
    assert migrated["query_type"] == "annotation"
    assert migrated["score"] == 0.0
    assert migrated["payload"]["dili_classification"] == "negative"
    assert migrated["payload"]["legacy_dili_negative_migrated"] is True


def test_epa_index_reads_bulk_summary_record_shape(tmp_path: Path) -> None:
    key = "BULK-AAAAA-BBBBBBBBBB-N"
    mapping = tmp_path / "bulk_mapping_all.jsonl"
    summary = tmp_path / "bulk_bioactivity_summary.jsonl"
    mapping.write_text(
        json.dumps(
            {
                "molecule_id": "T-BULK",
                "dtxsid": "DTXSID-BULK",
                "mapping_status": "exact_identifier_match",
                "mapping_basis": "original_inchikey",
                "mapping_value": key,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    summary.write_text(
        json.dumps(
            {
                "molecule_id": "T-BULK",
                "dtxsid": "DTXSID-BULK",
                "status": "returned",
                "record": [
                    {
                        "activeMc": 3,
                        "totalMc": 20,
                        "activeSc": 1,
                        "totalSc": 2,
                        "nhit": 0,
                        "cytotoxLowerUm": 1000.0,
                        "cytotoxMedianUm": 1000.0,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    index = EPAContextIndex.from_paths([mapping, summary])
    entry = index.lookup(inchikey=key)
    assert entry is not None
    assert entry["active_hit_count"] == 4
    assert entry["bioactivity_record_count"] == 22
    assert entry["bioactivity_signal"] is True
    assert entry["nhit"] == 0.0
    assert entry["cytotox_lower_um"] == 1000.0


def test_epa_index_does_not_replace_explicit_zero_threshold_count(
    tmp_path: Path,
) -> None:
    key = "ZERO-AAAAA-BBBBBBBBBB-N"
    mapping = tmp_path / "candidate_mapping.jsonl"
    summary = tmp_path / "candidate_risk_summary.jsonl"
    mapping.write_text(
        json.dumps(
            {
                "dtxsid": "DTXSID-ZERO",
                "mapping_status": "exact_identifier_match",
                "mapping_value": key,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    summary.write_text(
        json.dumps(
            {
                "dtxsid": "DTXSID-ZERO",
                "status": "returned",
                "active_hit_count_hitc_gte_0_9": 0,
                "record": [{"activeMc": 8, "totalMc": 20}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    index = EPAContextIndex.from_paths([mapping, summary])
    entry = index.lookup(inchikey=key)
    assert entry is not None
    assert entry["active_hit_count"] == 0
    assert entry["risk_signal"] is False
