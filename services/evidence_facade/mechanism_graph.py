"""Non-scoring candidate → target → disease → pathway evidence graph.

The graph deliberately separates a candidate-level hypothesis from external
target context.  A target being associated with MASLD does not prove that a
candidate binds or modulates that target, so all structure-derived candidate
edges remain ``hypothesis`` edges and cannot affect ranking.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from packages.models import ScoreRecord
from services.pipeline.config_loader import ROOT

CONTEXT_PATH = ROOT / "data" / "evidence_snapshot" / "v2" / "mechanism_context_v1.json"


@dataclass(frozen=True)
class MechanismEdge:
    source: str
    target: str
    relation: str
    evidence_level: str
    directness: str
    evidence_role: str
    evidence_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class MechanismGraph:
    molecule_id: str
    inchikey: str
    target_symbol: str | None
    chain_status: str
    context_snapshot_sha256: str
    edges: tuple[MechanismEdge, ...] = ()
    evidence_gaps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["edges"] = [asdict(edge) for edge in self.edges]
        return payload


def load_mechanism_context(path: Path | None = None) -> tuple[dict[str, Any], str]:
    """Load the frozen target-context snapshot and return its content hash."""
    resolved = Path(path or CONTEXT_PATH)
    if not resolved.is_file():
        return {}, ""
    content = resolved.read_bytes()
    return json.loads(content.decode("utf-8")), hashlib.sha256(content).hexdigest()


def _candidate_target(molecule: ScoreRecord) -> str | None:
    text = f"{molecule.selection_reason} {molecule.overall_reason}".upper()
    if "FXR" in text:
        return "NR1H4"
    if "AMPK" in text:
        return "PRKAA1"
    return None


def _target_edges(target_symbol: str, target: dict[str, Any], disease: dict[str, Any]) -> list[MechanismEdge]:
    target_id = f"target:{target_symbol}"
    disease_id = f"disease:{disease.get('id', '')}"
    datasource_scores = target.get("masld_context", {}).get("datasource_scores", {})
    context_id = f"opentargets:{target.get('ensembl_id', target_symbol)}:{disease.get('id', '')}"
    edges = [
        MechanismEdge(
            source=target_id,
            target=disease_id,
            relation="associated_with",
            evidence_level="L1",
            directness="context",
            evidence_role="disease_context",
            evidence_ids=(context_id,),
            notes=(
                "Open Targets association context; datasource score is not candidate binding, "
                "causal proof, or direction of effect.",
                f"datasource_scores={json.dumps(datasource_scores, sort_keys=True)}",
            ),
        )
    ]
    for pathway in target.get("pathways") or []:
        edges.append(
            MechanismEdge(
                source=target_id,
                target=f"pathway:{pathway.get('stable_id', '')}",
                relation="participates_in",
                evidence_level="L1",
                directness="context",
                evidence_role="pathway_context",
                evidence_ids=(f"reactome:{pathway.get('stable_id', '')}",),
                notes=(
                    str(pathway.get("name") or ""),
                    f"stable_id_version={pathway.get('stable_id_version', '')}",
                ),
            )
        )
    return edges


def build_mechanism_graphs(
    top: list[ScoreRecord],
    *,
    context: dict[str, Any] | None = None,
    context_sha256: str = "",
) -> list[MechanismGraph]:
    """Build graphs without changing any score, eligibility, or ordering."""
    context = context or {}
    disease = context.get("disease") or {}
    targets = context.get("targets") or {}
    graphs: list[MechanismGraph] = []
    for molecule in top:
        symbol = _candidate_target(molecule)
        if not symbol:
            graphs.append(
                MechanismGraph(
                    molecule_id=molecule.molecule_id,
                    inchikey=molecule.inchikey,
                    target_symbol=None,
                    chain_status="unresolved",
                    context_snapshot_sha256=context_sha256,
                    evidence_gaps=(
                        "candidate_target_evidence_missing",
                        "target_disease_context_not_selected",
                        "target_pathway_context_not_selected",
                    ),
                )
            )
            continue

        target = targets.get(symbol) or {}
        edges: list[MechanismEdge] = [
            MechanismEdge(
                source=f"candidate:{molecule.molecule_id}",
                target=f"target:{symbol}",
                relation="hypothesized_from_structure_and_reference_similarity",
                evidence_level="L3",
                directness="hypothesis",
                evidence_role="candidate_mechanism_hypothesis",
                evidence_ids=(f"molmind:structure_similarity:{molecule.molecule_id}",),
                notes=(
                    "Not a candidate binding or perturbation measurement; requires experiment.",
                ),
            )
        ]
        edges.extend(_target_edges(symbol, target, disease))
        gaps = ["candidate_target_direct_evidence_missing"]
        if not target:
            gaps.append("target_context_snapshot_missing")
        if not disease:
            gaps.append("disease_context_snapshot_missing")
        status = "hypothesis_only" if target and disease else "context_incomplete"
        graphs.append(
            MechanismGraph(
                molecule_id=molecule.molecule_id,
                inchikey=molecule.inchikey,
                target_symbol=symbol,
                chain_status=status,
                context_snapshot_sha256=context_sha256,
                edges=tuple(edges),
                evidence_gaps=tuple(gaps),
            )
        )
    return graphs


def serialize_mechanism_graphs(graphs: list[MechanismGraph]) -> list[dict[str, Any]]:
    return [graph.to_dict() for graph in graphs]

