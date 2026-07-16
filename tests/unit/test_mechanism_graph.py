"""候选→靶点→疾病→通路图：只用于可追溯机制叙述，不参与评分。"""

from __future__ import annotations

import json
from pathlib import Path

from packages.models import ScoreRecord
from services.evidence_facade.mechanism_graph import (
    build_mechanism_graphs,
    load_mechanism_context,
)
from services.pipeline.export import export_mechanism_graph_json

ROOT = Path(__file__).resolve().parents[2]


def _candidate(molecule_id: str, reason: str) -> ScoreRecord:
    return ScoreRecord(
        molecule_id=molecule_id,
        smiles="CCO",
        inchikey=f"{molecule_id}-AAAA-BBBBBBBBBB-N",
        cas=None,
        scaffold_smiles="",
        lipid_score=0.5,
        tox_risk=0.1,
        novelty_score=0.5,
        conf_e=0.0,
        final_score=0.5,
        tox_heads={},
        lipid_parts={},
        attributions=[],
        lipid_rationale="proxy",
        tox_rationale="proxy",
        overall_reason=reason,
        eligibility_status="eligible",
    )


def test_graph_keeps_candidate_target_as_hypothesis_only() -> None:
    context, context_hash = load_mechanism_context()
    graphs = build_mechanism_graphs(
        [_candidate("FXR1", "S_final=0.5; critic_quota pathway=FXR")],
        context=context,
        context_sha256=context_hash,
    )

    graph = graphs[0]
    assert graph.target_symbol == "NR1H4"
    assert graph.chain_status == "hypothesis_only"
    candidate_edge = graph.edges[0]
    assert candidate_edge.directness == "hypothesis"
    assert candidate_edge.evidence_level == "L3"
    assert "candidate_target_direct_evidence_missing" in graph.evidence_gaps
    assert any(edge.evidence_role == "disease_context" for edge in graph.edges)
    assert any(edge.evidence_role == "pathway_context" for edge in graph.edges)


def test_unresolved_candidate_has_no_fabricated_target_edge() -> None:
    context, context_hash = load_mechanism_context()
    graph = build_mechanism_graphs(
        [_candidate("UNRESOLVED1", "S_final=0.5; critic_quota pathway=UNRESOLVED")],
        context=context,
        context_sha256=context_hash,
    )[0]
    assert graph.target_symbol is None
    assert graph.chain_status == "unresolved"
    assert graph.edges == ()
    assert "candidate_target_evidence_missing" in graph.evidence_gaps


def test_graph_export_is_deterministic_and_non_scoring(tmp_path: Path) -> None:
    context, context_hash = load_mechanism_context()
    graphs = build_mechanism_graphs(
        [_candidate("AMPK1", "S_final=0.5; critic_quota pathway=AMPK")],
        context=context,
        context_sha256=context_hash,
    )
    out = tmp_path / "mechanism_graph.json"
    export_mechanism_graph_json(graphs, out)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["ranking_effect"] == "none"
    assert payload["graphs"][0]["chain_status"] == "hypothesis_only"
    assert "final_score" not in payload["graphs"][0]
