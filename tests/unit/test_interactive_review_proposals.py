"""Interactive nomination-review proposals (Web human confirm path)."""

from __future__ import annotations

from packages.models import ScoreRecord
from services.nomination import (
    apply_selected_proposals,
    build_interactive_review_proposals,
    get_review_session,
    store_review_session,
)


def _score(
    molecule_id: str,
    *,
    final_score: float = 0.5,
    alert: float = 0.0,
    evidence: float = 0.0,
    epa: dict | None = None,
    factors: dict | None = None,
) -> ScoreRecord:
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
        tox_heads={"alert": alert, "evidence": evidence},
        lipid_parts={},
        attributions=[],
        lipid_rationale="",
        tox_rationale="",
        overall_reason="",
        eligibility_status="eligible",
        eligibility_reasons=("lipid_and_toxicity_policy_passed",),
        gated_out=False,
        epa_audit=dict(epa or {}),
        selection_factors=dict(factors or {}),
    )


def test_build_proposals_from_structure_alert_and_identity() -> None:
    top = [
        _score("A1", final_score=0.9, alert=0.9),
        _score(
            "A2",
            final_score=0.8,
            evidence=0.32,
            epa={
                "mapping_basis": "cas",
                "query_status": "identity_review_required",
                "mapping_status": "identifier_match_requires_structure_audit",
                "cytotox_risk_tier": "weak_risk_review",
            },
        ),
        _score("A3", final_score=0.7, alert=0.25),
    ]
    reserve = [_score("R1", final_score=0.7), _score("R2", final_score=0.6)]
    bundle = build_interactive_review_proposals(top, reserve, use_llm=False)
    assert bundle.enabled is True
    assert bundle.llm_used is False
    assert bundle.draft_engine == "rules"
    assert any(p.suggested_action == "drop_from_primary" for p in bundle.proposals)
    drop = next(p for p in bundle.proposals if p.suggested_action == "drop_from_primary")
    assert drop.molecule_id == "A1"
    assert drop.replacement_molecule_id == "R1"
    assert any(p.issue_type == "identity_audit" for p in bundle.proposals)
    assert any(p.issue_type == "epa_weak_risk_review" for p in bundle.proposals)
    assert any(
        p.molecule_id == "A3" and p.issue_type == "structure_alert" for p in bundle.proposals
    )


def test_apply_selected_drop_promotes_reserve() -> None:
    top = [_score("A1", final_score=0.9, alert=0.9), _score("A2", final_score=0.8)]
    reserve = [_score("R1", final_score=0.7)]
    bundle = build_interactive_review_proposals(top, reserve)
    drop = next(p for p in bundle.proposals if p.suggested_action == "drop_from_primary")
    applied = apply_selected_proposals(
        top=top,
        reserve=reserve,
        proposals=bundle.proposals,
        selected_proposal_ids=[drop.proposal_id],
        top_n=2,
        reserve_n=1,
    )
    # R1 is promoted then re-sorted by score (A2 0.8 > R1 0.7), not seat fill.
    assert [m.molecule_id for m in applied.top] == ["A2", "R1"]
    assert applied.top[1].replacement_for == "A1"
    assert applied.top[1].primary_rank == 2
    assert applied.top[1].selection_factors.get("interactive_review") == "promoted_from_reserve"


def test_unselected_proposals_do_not_change_board() -> None:
    top = [_score("A1", final_score=0.9, alert=0.9), _score("A2", final_score=0.8)]
    reserve = [_score("R1", final_score=0.7)]
    bundle = build_interactive_review_proposals(top, reserve)
    applied = apply_selected_proposals(
        top=top,
        reserve=reserve,
        proposals=bundle.proposals,
        selected_proposal_ids=[],
        top_n=2,
        reserve_n=1,
    )
    assert [m.molecule_id for m in applied.top] == ["A1", "A2"]
    assert applied.applied_proposal_ids == []


def test_review_session_roundtrip() -> None:
    top = [_score("A1", final_score=0.9, alert=0.6)]
    reserve = [_score("R1", final_score=0.7)]
    bundle = build_interactive_review_proposals(top, reserve)
    assert bundle.proposals
    store_review_session(
        "run-test-1",
        top=top,
        reserve=reserve,
        proposals=bundle.proposals,
        mode="offline",
        config_hash="abc",
        input_sha256="def",
        summary={"run_id": "run-test-1"},
        source_filename="sample.sdf",
        llm_cfg={"enabled": False},
        assumptions={"note": "unit"},
        hepg2_ffa_resources={"ranking_effect": "none"},
        logs=[{"level": "INFO", "message": "hi"}],
    )
    session = get_review_session("run-test-1")
    assert session is not None
    assert session["mode"] == "offline"
    assert isinstance(session["proposals"][0], dict)
    assert session["source_filename"] == "sample.sdf"
    assert session["llm_cfg"]["enabled"] is False
    assert session["hepg2_ffa_resources"]["ranking_effect"] == "none"
    assert session["logs"][0]["message"] == "hi"


def test_review_session_survives_memory_clear(monkeypatch, tmp_path) -> None:
    from services.nomination import proposals as prop_mod

    monkeypatch.setenv("MOLMIND_REVIEW_SESSION_DIR", str(tmp_path))
    top = [_score("A1", final_score=0.9, alert=0.9)]
    reserve = [_score("R1", final_score=0.7)]
    bundle = build_interactive_review_proposals(top, reserve)
    store_review_session(
        "run-disk-1",
        top=top,
        reserve=reserve,
        proposals=bundle.proposals,
        mode="auto",
        config_hash="hash",
        input_sha256="sha",
    )
    # Simulate another worker / reload: memory empty, disk remains.
    with prop_mod._SESSION_LOCK:
        prop_mod._REVIEW_SESSIONS.clear()
    session = get_review_session("run-disk-1")
    assert session is not None
    assert [m.molecule_id for m in session["top"]] == ["A1"]
