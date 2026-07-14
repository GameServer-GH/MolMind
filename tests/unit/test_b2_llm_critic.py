"""B2：证据约束 LLM Critic（ChemCrow 式；默关；非法建议不改榜）。"""

from __future__ import annotations

from packages.models import Attribution, CriticAction, ScoreRecord
from services.critic import (
    apply_llm_critic_suggestions,
    collect_run_evidence_ids,
    filter_suggestions_by_run_evidence,
    run_evidence_bound_llm_critic,
)
from services.pipeline.config_loader import load_config


def _mol(mid: str, eids: list[str] | None = None) -> ScoreRecord:
    attrs = [
        Attribution("evidence", "chembl", value=0.5, evidence_id=eid) for eid in (eids or [])
    ]
    return ScoreRecord(
        molecule_id=mid,
        smiles="CCO",
        inchikey="",
        cas=None,
        scaffold_smiles="",
        lipid_score=0.5,
        tox_risk=0.2,
        novelty_score=0.5,
        conf_e=0.4 if eids else 0.0,
        final_score=0.5,
        tox_heads={},
        lipid_parts={},
        attributions=attrs,
        lipid_rationale="",
        tox_rationale="",
        overall_reason="",
    )


def test_collect_run_evidence_ids() -> None:
    top = [_mol("A", ["chembl:1"]), _mol("B", ["pubchem:2"]), _mol("C")]
    ids = collect_run_evidence_ids(top)
    assert ids == {"chembl:1", "pubchem:2"}


def test_filter_rejects_unknown_and_empty_drop() -> None:
    allowed = {"chembl:1"}
    ok = CriticAction(action="drop", molecule_id="A", reason="tox", evidence_ids=["chembl:1"])
    bad_empty = CriticAction(action="drop", molecule_id="B", reason="x", evidence_ids=[])
    bad_id = CriticAction(action="drop", molecule_id="C", reason="x", evidence_ids=["fake:9"])
    accepted, rejected = filter_suggestions_by_run_evidence([ok, bad_empty, bad_id], allowed)
    assert len(accepted) == 1 and accepted[0].molecule_id == "A"
    assert {r.molecule_id for r in rejected} == {"B", "C"}


def test_apply_drop_only_with_valid_evidence() -> None:
    top = [_mol("A", ["chembl:1"]), _mol("B", ["chembl:1"])]
    allowed = collect_run_evidence_ids(top)
    illegal = [CriticAction(action="drop", molecule_id="A", reason="no", evidence_ids=[])]
    kept = apply_llm_critic_suggestions(
        top, illegal, affect_ranking=True, allowed_evidence_ids=allowed
    )
    assert [m.molecule_id for m in kept] == ["A", "B"]

    legal = [CriticAction(action="drop", molecule_id="A", reason="ok", evidence_ids=["chembl:1"])]
    kept2 = apply_llm_critic_suggestions(
        top, legal, affect_ranking=True, allowed_evidence_ids=allowed
    )
    assert [m.molecule_id for m in kept2] == ["B"]


def test_default_critic_off_no_actions() -> None:
    cfg = load_config(mode="offline")
    assert cfg.llm_critic_enabled is False
    top = [_mol("A", ["chembl:1"])]
    new_top, actions = run_evidence_bound_llm_critic(top, cfg)
    assert actions == []
    assert new_top == top


def test_enabled_stub_audits_keep_without_rank_change() -> None:
    cfg = load_config(mode="offline")
    cfg.raw["llm"] = {
        "enabled": True,
        "critic_enabled": True,
        "critic_affects_ranking": False,
    }
    top = [_mol("A", ["chembl:1"])]
    new_top, actions = run_evidence_bound_llm_critic(top, cfg)
    assert any(a.action == "keep" and a.evidence_ids for a in actions)
    assert [m.molecule_id for m in new_top] == ["A"]
