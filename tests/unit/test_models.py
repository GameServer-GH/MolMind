"""packages.models：序列化字段齐全；禁止 SI/EC50 等 ADR-M16 字段名。"""

from __future__ import annotations

from dataclasses import fields

from packages.models import (
    Attribution,
    CriticAction,
    EvidenceHit,
    FilterDecision,
    MoleculeRecord,
    RunDiagnostics,
    ScoreRecord,
    assert_no_forbidden_fields,
    serialize_record,
)
from packages.models.records import FORBIDDEN_FIELD_NAMES


def test_molecule_record_serialize_has_core_fields() -> None:
    rec = MoleculeRecord(
        molecule_id="T1",
        smiles="CCO",
        inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
        cas=None,
        mw=46.07,
        logp=-0.1,
        hbd=1,
        hba=1,
        tpsa=20.2,
        rotatable_bonds=0,
        aromatic_rings=0,
        fp_bits=object(),
    )
    data = serialize_record(rec)
    for key in (
        "molecule_id",
        "smiles",
        "inchikey",
        "mw",
        "logp",
        "hbd",
        "hba",
        "tpsa",
        "rotatable_bonds",
        "aromatic_rings",
    ):
        assert key in data
    assert "fp_bits" not in data


def test_score_record_serialize_has_score_fields() -> None:
    rec = ScoreRecord(
        molecule_id="T1",
        smiles="CCO",
        inchikey="X",
        cas=None,
        scaffold_smiles="CCO",
        lipid_score=0.5,
        tox_risk=0.2,
        novelty_score=0.8,
        conf_e=0.1,
        final_score=0.6,
        tox_heads={"alert": 0.1},
        lipid_parts={"rule": 0.5},
        attributions=[Attribution(source="rule", detail="demo", value=0.5)],
        lipid_rationale="r",
        tox_rationale="t",
        overall_reason="o",
    )
    data = serialize_record(rec)
    for key in (
        "lipid_score",
        "tox_risk",
        "novelty_score",
        "conf_e",
        "final_score",
        "lipid_rationale",
        "tox_rationale",
        "overall_reason",
        "attributions",
    ):
        assert key in data
    assert data["attributions"][0]["source"] == "rule"


def test_evidence_hit_and_critic_action_fields() -> None:
    hit = EvidenceHit(
        adapter_id="chembl_lipid_v1",
        query_type="lipid",
        score=0.7,
        confidence=0.8,
        evidence_id="CHEMBL123",
    )
    assert serialize_record(hit)["evidence_id"] == "CHEMBL123"
    action = CriticAction(action="drop", molecule_id="T1", reason="fp")
    assert serialize_record(action)["action"] == "drop"


def test_no_adr_m16_forbidden_field_names() -> None:
    for cls in (
        MoleculeRecord,
        FilterDecision,
        Attribution,
        EvidenceHit,
        ScoreRecord,
        CriticAction,
        RunDiagnostics,
    ):
        assert_no_forbidden_fields(cls)
        names = {f.name.lower() for f in fields(cls)}
        assert not (names & FORBIDDEN_FIELD_NAMES)
