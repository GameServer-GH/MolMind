"""Clinical exclusion and nomination-review reproducibility."""

from __future__ import annotations

from packages.chem_core import compute_descriptors, morgan_fp
from packages.models import MoleculeRecord, ScoreRecord
from services.nomination import (
    apply_clinical_exclusion_to_score,
    apply_nomination_review,
    load_clinical_exclusions,
    match_clinical_exclusion,
)
from services.pipeline.config_loader import load_config


def _record(
    *,
    molecule_id: str,
    smiles: str = "CCO",
    inchikey: str = "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
    cas: str | None = "64-17-5",
) -> MoleculeRecord:
    desc = compute_descriptors(smiles)
    assert desc is not None
    from rdkit import Chem

    mol = Chem.MolFromSmiles(smiles)
    assert mol is not None
    return MoleculeRecord(
        molecule_id=molecule_id,
        smiles=smiles,
        inchikey=inchikey or (Chem.MolToInchiKey(mol) or ""),
        cas=cas,
        mw=float(desc["mw"]),
        logp=float(desc["logp"]),
        hbd=int(desc["hbd"]),
        hba=int(desc["hba"]),
        tpsa=float(desc["tpsa"]),
        rotatable_bonds=int(desc["rotatable_bonds"]),
        aromatic_rings=int(desc["aromatic_rings"]),
        fp_bits=morgan_fp(mol),
    )


def _score(molecule_id: str, *, final_score: float = 0.5) -> ScoreRecord:
    return ScoreRecord(
        molecule_id=molecule_id,
        smiles="CCO",
        inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
        cas=None,
        scaffold_smiles="CCO",
        lipid_score=0.4,
        tox_risk=0.2,
        novelty_score=0.5,
        conf_e=0.0,
        final_score=final_score,
        selection_score=final_score,
        tox_heads={},
        lipid_parts={},
        attributions=[],
        lipid_rationale="",
        tox_rationale="",
        overall_reason="",
        eligibility_status="eligible",
        eligibility_reasons=("lipid_and_toxicity_policy_passed",),
        gated_out=False,
    )


def test_clinical_exclusion_matches_zomepirac_identity() -> None:
    cfg = load_config(mode="offline")
    exclusions = load_clinical_exclusions(cfg.clinical_exclusions)
    record = _record(
        molecule_id="T0264",
        smiles="Cc1cc(CC(=O)O)n(C)c1C(=O)c1ccc(Cl)cc1",
        inchikey="ZXVNMYWKKDOREA-UHFFFAOYSA-N",
        cas="64092-48-4",
    )
    hit = match_clinical_exclusion(record, exclusions)
    assert hit is not None
    assert hit.exclusion_id == "zomepirac_withdrawn_anaphylaxis"

    scored = _score("T0264")
    action = apply_clinical_exclusion_to_score(scored, record, exclusions)
    assert action is not None
    assert scored.gated_out is True
    assert scored.eligibility_status == "ineligible"
    assert "clinical_exclusion" in scored.eligibility_reasons


def test_nomination_review_drop_promotes_reserve() -> None:
    top = [_score("A", final_score=0.9), _score("B", final_score=0.8), _score("C", final_score=0.7)]
    reserve = [_score("D", final_score=0.6), _score("E", final_score=0.5)]
    leftover = [_score("F", final_score=0.4)]
    result = apply_nomination_review(
        algorithmic_top=top,
        algorithmic_reserve=reserve,
        leftover_pool=leftover,
        review_config={
            "enabled": True,
            "require_input_match": False,
            "apply_in_modes": ["offline"],
            "decisions": [
                {
                    "molecule_id": "B",
                    "action": "drop_from_primary",
                    "reason": "manual drop for test",
                }
            ],
        },
        mode="offline",
        top_n=3,
        reserve_n=2,
        input_sha256="any",
    )
    # D is promoted then the board is re-sorted by selection_score (not seat fill).
    assert [m.molecule_id for m in result.nominated_top] == ["A", "C", "D"]
    assert result.nominated_top[2].replacement_for == "B"
    assert result.nominated_top[2].primary_rank == 3
    assert [m.molecule_id for m in result.nominated_reserve] == ["E", "F"]
    assert any(a.applied and a.replaced_by == "D" for a in result.actions)


def test_nomination_review_skips_when_input_hash_mismatches() -> None:
    top = [_score("T19959", final_score=0.9)]
    reserve = [_score("X", final_score=0.5)]
    result = apply_nomination_review(
        algorithmic_top=top,
        algorithmic_reserve=reserve,
        leftover_pool=[],
        review_config={
            "enabled": True,
            "require_input_match": True,
            "applies_to_input_sha256": ["aaa"],
            "apply_in_modes": ["offline"],
            "decisions": [
                {
                    "molecule_id": "T19959",
                    "action": "drop_from_primary",
                    "reason": "should not apply on other sdf",
                }
            ],
        },
        mode="offline",
        top_n=1,
        reserve_n=1,
        input_sha256="bbb",
    )
    assert result.review_applied is False
    assert result.input_matched is False
    assert [m.molecule_id for m in result.nominated_top] == ["T19959"]
    assert result.actions[0].action == "skip_review_bundle"


def test_nomination_review_resolves_by_inchikey_across_ids() -> None:
    alien = _score("OTHER_ID", final_score=0.9)
    alien.inchikey = "ABOOPXYCKNFDNJ-UHFFFAOYSA-N"
    reserve = [_score("R1", final_score=0.4)]
    result = apply_nomination_review(
        algorithmic_top=[alien],
        algorithmic_reserve=reserve,
        leftover_pool=[],
        review_config={
            "enabled": True,
            "require_input_match": False,
            "apply_in_modes": ["offline"],
            "decisions": [
                {
                    "molecule_id": "T19959",
                    "inchikeys": ["ABOOPXYCKNFDNJ-UHFFFAOYSA-N"],
                    "action": "drop_from_primary",
                    "reason": "identity portable drop",
                }
            ],
        },
        mode="offline",
        top_n=1,
        reserve_n=1,
        input_sha256="any",
    )
    assert [m.molecule_id for m in result.nominated_top] == ["R1"]
    assert result.actions[0].molecule_id == "OTHER_ID"
    assert result.actions[0].applied is True


def test_config_loads_clinical_and_nomination_review() -> None:
    cfg = load_config(mode="offline")
    assert cfg.clinical_exclusions.get("enabled") is True
    assert any(
        row.get("id") == "zomepirac_withdrawn_anaphylaxis"
        for row in cfg.clinical_exclusions.get("exclusions") or []
    )
    assert cfg.nomination_review.get("enabled") is True
    assert cfg.nomination_review.get("require_input_match") is True
    assert cfg.nomination_review.get("applies_to_input_sha256")
