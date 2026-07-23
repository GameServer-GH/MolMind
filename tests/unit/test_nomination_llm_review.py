"""LLM nomination-review draft parsing and fallback."""

from __future__ import annotations

from unittest.mock import patch

from packages.models import ScoreRecord
from services.nomination import build_interactive_review_proposals
from services.nomination.llm_review import (
    parse_llm_review_payload,
    seats_to_narrative_markdown,
)


def _score(molecule_id: str, *, alert: float = 0.0) -> ScoreRecord:
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
        final_score=0.5,
        selection_score=0.5,
        tox_heads={"alert": alert, "evidence": 0.0},
        lipid_parts={},
        attributions=[],
        lipid_rationale="",
        tox_rationale="",
        overall_reason="",
        eligibility_status="eligible",
        eligibility_reasons=("lipid_and_toxicity_policy_passed",),
        gated_out=False,
    )


def test_parse_llm_review_payload_normalizes_seats() -> None:
    top = [_score("T1"), _score("T2"), _score("T3")]
    parsed = parse_llm_review_payload(
        {
            "conclusion": "可作为短名单，建议移出 T2",
            "intro": "按身份与 EPA 核过。",
            "seats": [
                {
                    "rank": 1,
                    "molecule_id": "T1",
                    "identity_label": "Foo",
                    "decision": "KEEP+NOTE",
                    "rationale": "需脚注",
                    "issue_types": ["identity_audit"],
                    "severity": "medium",
                },
                {
                    "rank": 2,
                    "molecule_id": "T2",
                    "identity_label": "Bar",
                    "decision": "drop",
                    "rationale": "撤市药史",
                    "issue_types": ["drug_history"],
                    "severity": "high",
                },
            ],
            "summary": {"extra_notes": ["主张上限 proxy"]},
        },
        top=top,
    )
    assert len(parsed["seats"]) == 3
    assert parsed["seats"][1]["decision"] == "DROP"
    assert parsed["seats"][2]["molecule_id"] == "T3"
    assert parsed["summary"]["drop"] == 1
    assert parsed["summary"]["keep_note"] >= 1
    md = seats_to_narrative_markdown(parsed)
    assert "T2" in md and "DROP" in md


def test_llm_bundle_maps_drop_and_annotate(monkeypatch) -> None:
    monkeypatch.setenv("MOLMIND_LLM_NOMINATION_REVIEW", "1")
    monkeypatch.setenv("MOLMIND_LLM_API_KEY", "sk-test")
    top = [_score("A1", alert=0.9), _score("A2"), _score("A3")]
    reserve = [_score("R1")]

    fake = {
        "conclusion": "建议移出 A1",
        "intro": "已逐条核对。",
        "seats": [
            {
                "rank": 1,
                "molecule_id": "A1",
                "identity_label": "Bad",
                "decision": "DROP",
                "rationale": "高警示",
                "issue_types": ["structure_alert"],
                "severity": "high",
            },
            {
                "rank": 2,
                "molecule_id": "A2",
                "identity_label": "Ok",
                "decision": "KEEP",
                "rationale": "无硬风险",
                "issue_types": [],
                "severity": "low",
            },
            {
                "rank": 3,
                "molecule_id": "A3",
                "identity_label": "Note",
                "decision": "KEEP+NOTE",
                "rationale": "需脚注",
                "issue_types": ["epa_weak_risk_review"],
                "severity": "medium",
            },
        ],
        "summary": {"extra_notes": ["proxy only"]},
    }

    with patch(
        "services.nomination.llm_review.run_llm_nomination_review",
        return_value={
            **fake,
            "narrative_markdown": seats_to_narrative_markdown(
                parse_llm_review_payload(fake, top=top)
            ),
            "summary": {
                "keep": 1,
                "keep_note": 1,
                "drop": 1,
                "extra_notes": ["proxy only"],
            },
            "seats": parse_llm_review_payload(fake, top=top)["seats"],
            "conclusion": fake["conclusion"],
            "intro": fake["intro"],
        },
    ):
        bundle = build_interactive_review_proposals(
            top,
            reserve,
            use_llm=True,
            llm_cfg={"enabled": True, "nomination_review": True},
        )

    assert bundle.llm_used is True
    assert bundle.draft_engine == "llm"
    assert bundle.conclusion.startswith("建议移出")
    assert len(bundle.seat_decisions) == 3
    actions = {p.molecule_id: p.suggested_action for p in bundle.proposals}
    assert actions["A1"] == "drop_from_primary"
    assert actions["A2"] == "keep"
    assert actions["A3"] == "annotate"
    drop = next(p for p in bundle.proposals if p.molecule_id == "A1")
    assert drop.replacement_molecule_id == "R1"
    assert drop.default_selected is True


def test_curated_hint_forces_zomepirac_drop() -> None:
    from services.nomination.llm_review import apply_curated_decision_overrides

    top = [
        _score("T0264"),
        _score("T19959"),
    ]
    parsed = {
        "conclusion": "全部 KEEP+NOTE",
        "intro": "已核过",
        "seats": [
            {
                "rank": 1,
                "molecule_id": "T0264",
                "identity_label": "无公共名",
                "decision": "KEEP+NOTE",
                "rationale": "EPA 空",
                "issue_types": ["claim_ceiling"],
                "severity": "low",
            },
            {
                "rank": 2,
                "molecule_id": "T19959",
                "identity_label": "Quizalofop",
                "decision": "KEEP+NOTE",
                "rationale": "proxy",
                "issue_types": ["claim_ceiling"],
                "severity": "medium",
            },
        ],
        "summary": {"keep": 0, "keep_note": 2, "drop": 0, "extra_notes": []},
    }
    out = apply_curated_decision_overrides(parsed, top)
    t0264 = next(s for s in out["seats"] if s["molecule_id"] == "T0264")
    assert t0264["decision"] == "DROP"
    assert t0264["severity"] == "high"
    assert out["summary"]["drop"] == 1
    assert "T0264" in out["conclusion"]


def test_llm_failure_falls_back_to_rules(monkeypatch) -> None:
    monkeypatch.setenv("MOLMIND_LLM_NOMINATION_REVIEW", "1")
    top = [_score("A1", alert=0.9), _score("A2")]
    reserve = [_score("R1")]
    with patch(
        "services.nomination.llm_review.run_llm_nomination_review",
        side_effect=RuntimeError("boom"),
    ):
        bundle = build_interactive_review_proposals(
            top,
            reserve,
            use_llm=True,
            llm_cfg={"enabled": True, "nomination_review": True},
        )
    assert bundle.llm_used is False
    assert bundle.draft_engine == "rules"
    assert "回退规则" in bundle.note
    assert any(p.suggested_action == "drop_from_primary" for p in bundle.proposals)
