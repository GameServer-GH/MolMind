"""DILIrank exact gate and sparse assay-grain scoring locks."""

from __future__ import annotations

import json
from pathlib import Path

from packages.goldset import load_goldset
from packages.models import MoleculeRecord
from services.evidence_facade.dilirank_gate import (
    audit_from_match,
    build_identity_rows,
    load_dilirank_index_from_config,
    load_identity_jsonl,
    normalize_concern,
    write_identity_jsonl,
)
from services.evidence_facade.facade import EvidenceFacade
from services.pipeline.config_loader import load_config
from services.public_data.assay_index import row_to_evidence_hit
from services.ranker.ranker import score_molecule


ROOT = Path(__file__).resolve().parents[2]


def _record(*, molecule_id: str, inchikey: str, smiles: str = "CCO") -> MoleculeRecord:
    return MoleculeRecord(
        molecule_id=molecule_id,
        smiles=smiles,
        inchikey=inchikey,
        cas=None,
        mw=46.0,
        logp=0.0,
        hbd=1,
        hba=1,
        tpsa=20.0,
        rotatable_bonds=0,
        aromatic_rings=0,
    )


def test_normalize_concern_variants() -> None:
    assert normalize_concern("vMost-DILI-concern") == "most"
    assert normalize_concern("vMOST-DILI-concern") == "most"
    assert normalize_concern("vLess-DILI-concern") == "less"
    assert normalize_concern("vNo-DILI-concern") == "no"
    assert normalize_concern("Ambiguous-DILI-concern") == "ambiguous"


def test_build_identity_rows_includes_reference_and_epa_matches() -> None:
    rows = build_identity_rows()
    assert len(rows) >= 39
    concerns = {row["concern"] for row in rows}
    assert "most" in concerns
    bases = {row["match_basis"] for row in rows}
    assert "reference_curated_inchikey" in bases
    assert "epa_preferred_name_exact" in bases


def test_write_identity_jsonl_roundtrip(tmp_path: Path) -> None:
    out = tmp_path / "identity_mapped.jsonl"
    summary = write_identity_jsonl(out)
    assert summary["row_count"] > 0
    index = load_identity_jsonl(out)
    assert index.size > 0


def test_dilirank_most_exact_hard_excludes(tmp_path: Path) -> None:
    # Amiodarone is curated Most in data/reference/dilirank.csv
    inchikey = "ONTTUONJMUPHEZ-UHFFFAOYSA-N"
    cfg = load_config(mode="offline")
    cfg.raw["evidence"]["dilirank_exact_gate"] = {
        "enabled": True,
        "hard_exclude_most": True,
        "identity_paths": [
            str(ROOT / "data/reference/dilirank.csv"),
        ],
    }
    cfg.evidence["dilirank_exact_gate"] = cfg.raw["evidence"]["dilirank_exact_gate"]
    # Disable EPA noise for this unit test.
    cfg.raw["evidence"]["epa_ctx"] = {"enabled": False, "integration_stage": 0}
    cfg.evidence["epa_ctx"] = cfg.raw["evidence"]["epa_ctx"]
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path / "empty")
    facade._dilirank_gate_index = load_dilirank_index_from_config(
        cfg.evidence["dilirank_exact_gate"]
    )
    record = _record(molecule_id="T-DILI-MOST", inchikey=inchikey)
    bundle = facade.query(
        inchikey=inchikey, cas=None, smiles=record.smiles, allow_live=False
    )
    assert bundle.dili_audit.get("action") == "hard_exclude"
    assert bundle.dili_audit.get("concern") == "most"
    scored = score_molecule(record, cfg, load_goldset(), bundle)
    assert scored.gated_out is True
    assert "dilirank_most_exact" in scored.eligibility_reasons


def test_dilirank_less_is_annotation_only(tmp_path: Path) -> None:
    identity = tmp_path / "dili.jsonl"
    identity.write_text(
        json.dumps(
            {
                "ltkb_id": "LT-TEST",
                "compound_name": "LessDrug",
                "concern": "less",
                "concern_raw": "vLess-DILI-concern",
                "inchikey": "BBBBBBBBBBBBBB-UHFFFAOYSA-N",
                "match_basis": "unit_test",
                "source": "unit",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    cfg = load_config(mode="offline")
    cfg.raw["evidence"]["dilirank_exact_gate"] = {
        "enabled": True,
        "hard_exclude_most": True,
        "identity_paths": [str(identity)],
    }
    cfg.evidence["dilirank_exact_gate"] = cfg.raw["evidence"]["dilirank_exact_gate"]
    cfg.raw["evidence"]["epa_ctx"] = {"enabled": False, "integration_stage": 0}
    cfg.evidence["epa_ctx"] = cfg.raw["evidence"]["epa_ctx"]
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path / "empty")
    facade._dilirank_gate_index = load_dilirank_index_from_config(
        cfg.evidence["dilirank_exact_gate"]
    )
    record = _record(molecule_id="T-DILI-LESS", inchikey="BBBBBBBBBBBBBB-UHFFFAOYSA-N")
    bundle = facade.query(
        inchikey=record.inchikey, cas=None, smiles=record.smiles, allow_live=False
    )
    assert bundle.dili_audit.get("action") == "annotate_only"
    assert bundle.dili_audit.get("concern") == "less"
    scored = score_molecule(record, cfg, load_goldset(), bundle)
    assert "dilirank_most_exact" not in scored.eligibility_reasons
    assert scored.dili_audit.get("ranking_effect") == "annotation_only"


def test_audit_from_match_disabled() -> None:
    audit = audit_from_match(None, enabled=False, hard_exclude_most=True)
    assert audit["status"] == "disabled"


def test_pubchem_and_bindingdb_assay_grain_never_score() -> None:
    pub = row_to_evidence_hit(
        {
            "qc_source": "pubchem_bioassay",
            "source_id": "pubchem_bioassay",
            "evidence_role": "task_evidence",
            "molmind_direction": "supports",
            "compound_id": "CID:1",
            "assay_id": "AID:1",
            "inchikey": "AAAAAAAAAAAAAA-UHFFFAOYSA-N",
        }
    )
    assert pub.score == 0.0
    assert pub.evidence_role == "annotation_only"

    bdb = row_to_evidence_hit(
        {
            "qc_source": "bindingdb",
            "source_id": "bindingdb",
            "evidence_role": "mechanism_support",
            "molmind_direction": "unknown",
            "compound_id": "BindingDB:1",
            "assay_id": "BindingDB:Q07869:Ki",
            "inchikey": "AAAAAAAAAAAAAA-UHFFFAOYSA-N",
        }
    )
    assert bdb.score == 0.0
    assert bdb.query_type == "pathway"

    chembl = row_to_evidence_hit(
        {
            "qc_source": "chembl_bioactivity",
            "source_id": "chembl_bioactivity",
            "evidence_role": "task_evidence",
            "molmind_direction": "supports",
            "compound_id": "CHEMBL1",
            "assay_id": "CHEMBL_ASSAY",
            "inchikey": "AAAAAAAAAAAAAA-UHFFFAOYSA-N",
        }
    )
    assert chembl.score > 0.0
    assert chembl.query_type == "lipid"


def test_evidence_source_audit_populated(tmp_path: Path) -> None:
    cfg = load_config(mode="offline")
    cfg.raw["evidence"]["epa_ctx"] = {"enabled": False, "integration_stage": 0}
    cfg.evidence["epa_ctx"] = cfg.raw["evidence"]["epa_ctx"]
    facade = EvidenceFacade(cfg, snapshot_dir=tmp_path / "empty")
    bundle = facade.query(
        inchikey="ZZZZZZZZZZZZZZ-UHFFFAOYSA-N",
        cas=None,
        smiles="CCO",
        allow_live=False,
    )
    assert "chembl" in bundle.evidence_source_audit
    assert "pubchem" in bundle.evidence_source_audit
    assert "bindingdb" in bundle.evidence_source_audit
    assert bundle.evidence_source_audit["bindingdb"]["ranking_effect"] in {
        "none",
        "annotation_or_audit_only",
    }
