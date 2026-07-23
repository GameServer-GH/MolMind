"""导出提名 CSV（禁止伪 SI/EC50/CC50 列）。

CSV schema lock · LJR
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from packages.models import CriticAction, ScoreRecord, ScreeningAuditRecord
from services.evidence_facade.mechanism_graph import MechanismGraph, serialize_mechanism_graphs

# export lineage: LJR — column order is part of the export contract
CSV_COLUMNS = [
    "排名",
    "化合物标识符",
    "降脂依据",
    "毒性判断",
    "排序理由",
    "rank",
    "molecule_id",
    "cas",
    "inchikey",
    "run_id",
    "input_sha256",
    "selection_sha256",
    "lipid_score",
    "tox_risk",
    "final_score",
    "selection_score",
    "effect_proxy_score",
    "novelty_proxy_score",
    "effect_rank",
    "novelty_rank",
    "effect_x_novelty",
    "effect_novelty_equal_mean",
    "competition_scoring_version",
    "novelty_score",
    "conf_e",
    "toxicity_confidence",
    "toxicity_uncertainty",
    "toxicity_model_applicability",
    "toxicity_evidence_coverage",
    "risk_signal_confidence",
    "safety_clearance_confidence",
    "tox_upper_bound",
    "eligibility_status",
    "eligibility_reasons",
    "tox_alert",
    "tox_physchem",
    "tox_dili",
    "tox_admet",
    "tox_evidence",
    "scaffold",
    "novelty_reference_version",
    "novelty_nearest_reference",
    "novelty_max_similarity",
    "internal_nearest_similarity",
    "similarity_cluster",
    "selection_tier",
    "selection_reason",
    "robust_inclusion_frequency",
    "robust_rank_median",
    "robust_rank_min",
    "robust_rank_max",
    "lipid_rationale",
    "tox_rationale",
    "overall_reason",
    "run_mode",
    "config_hash",
    "degraded_channels",
    "scientific_status",
    "claim_ceiling",
    "audit_missing",
    "lipid_evidence_status",
    "toxicity_evidence_status",
    "screening_concentration_um",
    "viability_endpoint",
    "viability_threshold_reference",
    "dual_endpoint_claim",
    "nomination_tier",
    "primary_rank",
    "reserve_rank",
    "replacement_for",
    "purchase_status",
    "solubility_status",
    "identity_status",
    "epa_stage",
    "epa_status",
    "epa_query_status",
    "epa_mapping_status",
    "epa_mapping_basis",
    "epa_dtxsid",
    "epa_active_hit_count",
    "epa_nhit",
    "epa_cytotox_lower_um",
    "epa_cytotox_risk_tier",
    "epa_bioactivity_record_count",
    "epa_risk_applied",
    "epa_risk_inherited_from_dtxsid",
    "dili_status",
    "dili_action",
    "dili_concern",
    "dili_match_basis",
    "dili_compound_name",
    "chembl_query_status",
    "pubchem_query_status",
    "bindingdb_query_status",
]

SCREENING_AUDIT_COLUMNS = [
    "source_index",
    "molecule_id",
    "status",
    "reason_codes",
    "reason",
    "alert_hits",
]

CRITIC_AUDIT_COLUMNS = [
    "molecule_id",
    "action",
    "original_status",
    "checks_performed",
    "reason",
    "score_before",
    "score_after",
    "eligibility_before",
    "eligibility_after",
    "rank_before",
    "rank_after",
    "final_decision",
    "evidence_ids",
]


def rows_from_top(
    molecules: list[ScoreRecord],
    *,
    mode: str,
    config_hash: str,
    degraded_channels: list[str],
    run_id: str = "",
    input_sha256: str = "",
    selection_hash: str = "",
) -> list[dict[str, str | int | float]]:
    if any(m.eligibility_status != "eligible" or m.gated_out for m in molecules):
        raise ValueError("导出结果只能包含 eligibility_status=eligible 的候选")
    ids = [m.molecule_id for m in molecules]
    if len(ids) != len(set(ids)):
        raise ValueError("导出结果候选标识符必须唯一")
    degraded = "|".join(degraded_channels) if degraded_channels else ""
    rows: list[dict[str, str | int | float]] = []
    for rank, mol in enumerate(molecules, start=1):
        heads = mol.tox_heads
        rows.append(
            {
                "排名": rank,
                "化合物标识符": mol.molecule_id,
                "降脂依据": mol.lipid_rationale,
                "毒性判断": mol.tox_rationale,
                "排序理由": mol.overall_reason,
                "rank": rank,
                "molecule_id": mol.molecule_id,
                "cas": mol.cas or "",
                "inchikey": mol.inchikey or "",
                "run_id": run_id,
                "input_sha256": input_sha256,
                "selection_sha256": selection_hash,
                "lipid_score": mol.lipid_score,
                "tox_risk": mol.tox_risk,
                "final_score": mol.final_score,
                "selection_score": mol.selection_score,
                "effect_proxy_score": mol.effect_proxy_score,
                "novelty_proxy_score": mol.novelty_proxy_score,
                "effect_rank": mol.effect_rank if mol.effect_rank is not None else "",
                "novelty_rank": mol.novelty_rank if mol.novelty_rank is not None else "",
                "effect_x_novelty": mol.effect_x_novelty,
                "effect_novelty_equal_mean": mol.effect_novelty_equal_mean,
                "competition_scoring_version": mol.competition_scoring_version,
                "novelty_score": mol.novelty_score,
                "conf_e": mol.conf_e,
                "toxicity_confidence": mol.toxicity_confidence,
                "toxicity_uncertainty": mol.toxicity_uncertainty,
                "toxicity_model_applicability": mol.toxicity_model_applicability,
                "toxicity_evidence_coverage": mol.toxicity_evidence_coverage,
                "risk_signal_confidence": mol.risk_signal_confidence,
                "safety_clearance_confidence": mol.safety_clearance_confidence,
                "tox_upper_bound": mol.tox_upper_bound,
                "eligibility_status": mol.eligibility_status,
                "eligibility_reasons": "|".join(mol.eligibility_reasons),
                "tox_alert": heads.get("alert", 0.0),
                "tox_physchem": heads.get("physchem", 0.0),
                "tox_dili": heads.get("dili", 0.0),
                "tox_admet": heads.get("admet", 0.0),
                "tox_evidence": heads.get("evidence", 0.0),
                "scaffold": mol.scaffold_smiles,
                "novelty_reference_version": mol.novelty_reference_version,
                "novelty_nearest_reference": mol.novelty_nearest_reference,
                "novelty_max_similarity": mol.novelty_max_similarity,
                "internal_nearest_similarity": mol.internal_nearest_similarity,
                "similarity_cluster": mol.similarity_cluster,
                "selection_tier": mol.selection_tier,
                "selection_reason": mol.selection_reason,
                "robust_inclusion_frequency": mol.robust_inclusion_frequency,
                "robust_rank_median": "" if mol.robust_rank_median is None else mol.robust_rank_median,
                "robust_rank_min": "" if mol.robust_rank_min is None else mol.robust_rank_min,
                "robust_rank_max": "" if mol.robust_rank_max is None else mol.robust_rank_max,
                "lipid_rationale": mol.lipid_rationale,
                "tox_rationale": mol.tox_rationale,
                "overall_reason": mol.overall_reason,
                "run_mode": mode,
                "config_hash": config_hash,
                "degraded_channels": degraded,
                "scientific_status": mol.scientific_status,
                "claim_ceiling": mol.claim_ceiling,
                "audit_missing": "|".join(mol.audit_missing),
                "lipid_evidence_status": mol.lipid_evidence_status,
                "toxicity_evidence_status": mol.toxicity_evidence_status,
                "screening_concentration_um": mol.screening_concentration_um,
                "viability_endpoint": mol.viability_endpoint,
                "viability_threshold_reference": mol.viability_threshold_reference,
                "dual_endpoint_claim": mol.dual_endpoint_claim,
                "nomination_tier": mol.nomination_tier,
                "primary_rank": mol.primary_rank if mol.primary_rank is not None else "",
                "reserve_rank": mol.reserve_rank if mol.reserve_rank is not None else "",
                "replacement_for": mol.replacement_for,
                "purchase_status": mol.purchase_status,
                "solubility_status": mol.solubility_status,
                "identity_status": mol.identity_status,
                "epa_stage": mol.epa_audit.get("stage", 0),
                "epa_status": mol.epa_audit.get("status", "disabled"),
                "epa_query_status": mol.epa_audit.get("query_status", "not_queried"),
                "epa_mapping_status": mol.epa_audit.get("mapping_status", "audit_missing"),
                "epa_mapping_basis": mol.epa_audit.get("mapping_basis", ""),
                "epa_dtxsid": mol.epa_audit.get("dtxsid", ""),
                "epa_active_hit_count": mol.epa_audit.get("active_hit_count", 0),
                "epa_nhit": mol.epa_audit.get("nhit", ""),
                "epa_cytotox_lower_um": mol.epa_audit.get("cytotox_lower_um", ""),
                "epa_cytotox_risk_tier": mol.epa_audit.get("cytotox_risk_tier", ""),
                "epa_bioactivity_record_count": mol.epa_audit.get("bioactivity_record_count", 0),
                "epa_risk_applied": bool(mol.epa_audit.get("risk_applied", False)),
                "epa_risk_inherited_from_dtxsid": mol.epa_audit.get(
                    "risk_inherited_from_dtxsid", ""
                ),
                "dili_status": (mol.dili_audit or {}).get("status", "disabled"),
                "dili_action": (mol.dili_audit or {}).get("action", "none"),
                "dili_concern": (mol.dili_audit or {}).get("concern", ""),
                "dili_match_basis": (mol.dili_audit or {}).get("match_basis", ""),
                "dili_compound_name": (mol.dili_audit or {}).get("compound_name", ""),
                "chembl_query_status": ((mol.evidence_source_audit or {}).get("chembl") or {}).get(
                    "status", "not_queried"
                ),
                "pubchem_query_status": ((mol.evidence_source_audit or {}).get("pubchem") or {}).get(
                    "status", "not_queried"
                ),
                "bindingdb_query_status": (
                    (mol.evidence_source_audit or {}).get("bindingdb") or {}
                ).get("status", "not_queried"),
            }
        )
    return rows


def to_csv_text(
    molecules: list[ScoreRecord],
    *,
    mode: str,
    config_hash: str,
    degraded_channels: list[str],
    run_id: str = "",
    input_sha256: str = "",
    selection_hash: str = "",
) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for row in rows_from_top(
        molecules,
        mode=mode,
        config_hash=config_hash,
        degraded_channels=degraded_channels,
        run_id=run_id,
        input_sha256=input_sha256,
        selection_hash=selection_hash,
    ):
        writer.writerow(row)
    return buffer.getvalue()


def export_nomination_csv(
    molecules: list[ScoreRecord],
    output_path: str | Path,
    *,
    mode: str,
    config_hash: str,
    degraded_channels: list[str],
    requested_top_n: int,
    run_id: str = "",
    input_sha256: str = "",
    selection_hash: str = "",
) -> Path:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows_from_top(
            molecules,
            mode=mode,
            config_hash=config_hash,
            degraded_channels=degraded_channels,
            run_id=run_id,
            input_sha256=input_sha256,
            selection_hash=selection_hash,
        ):
            writer.writerow(row)

    if len(molecules) < requested_top_n:
        note_path = out.with_suffix(".note.txt")
        note_path.write_text(
            f"合格候选仅 {len(molecules)} 个，少于请求的 Top {requested_top_n}。\n",
            encoding="utf-8",
        )
    return out


def export_screening_audit_csv(
    audit_records: list[ScreeningAuditRecord],
    output_path: str | Path,
) -> Path:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=SCREENING_AUDIT_COLUMNS)
        writer.writeheader()
        for item in sorted(audit_records, key=lambda x: (x.source_index, x.molecule_id)):
            writer.writerow(
                {
                    "source_index": item.source_index,
                    "molecule_id": item.molecule_id,
                    "status": item.status,
                    "reason_codes": "|".join(item.reason_codes),
                    "reason": item.reason,
                    "alert_hits": "|".join(item.alert_hits),
                }
            )
    return out


def export_critic_audit_csv(actions: list[CriticAction], output_path: str | Path) -> Path:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CRITIC_AUDIT_COLUMNS)
        writer.writeheader()
        for item in actions:
            writer.writerow(
                {
                    "molecule_id": item.molecule_id,
                    "action": item.action,
                    "original_status": item.original_status,
                    "checks_performed": "|".join(item.checks_performed),
                    "reason": item.reason,
                    "score_before": "" if item.score_before is None else item.score_before,
                    "score_after": "" if item.score_after is None else item.score_after,
                    "eligibility_before": item.eligibility_before,
                    "eligibility_after": item.eligibility_after,
                    "rank_before": "" if item.rank_before is None else item.rank_before,
                    "rank_after": "" if item.rank_after is None else item.rank_after,
                    "final_decision": item.final_decision,
                    "evidence_ids": "|".join(item.evidence_ids),
                }
            )
    return out


def export_rank_robustness_json(rows: list[dict[str, object]], output_path: str | Path) -> Path:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def export_candidate_scores_jsonl(
    molecules: list[ScoreRecord], output_path: str | Path
) -> Path:
    """Export every scored candidate, including those not selected for Top10."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for molecule in molecules:
            payload = {
                "schema_version": "candidate-score-v1",
                "molecule_id": molecule.molecule_id,
                "smiles": molecule.smiles,
                "inchikey": molecule.inchikey,
                "lipid_score": molecule.lipid_score,
                "selection_score": molecule.selection_score,
                "effect_proxy_score": molecule.effect_proxy_score,
                "novelty_proxy_score": molecule.novelty_proxy_score,
                "effect_rank": molecule.effect_rank,
                "novelty_rank": molecule.novelty_rank,
                "effect_x_novelty": molecule.effect_x_novelty,
                "effect_novelty_equal_mean": molecule.effect_novelty_equal_mean,
                "competition_scoring_version": molecule.competition_scoring_version,
                "tox_risk": molecule.tox_risk,
                "tox_upper_bound": molecule.tox_upper_bound,
                "final_score": molecule.final_score,
                "eligibility_status": molecule.eligibility_status,
                "scientific_status": molecule.scientific_status,
                "claim_ceiling": molecule.claim_ceiling,
                "audit_missing": list(molecule.audit_missing),
                "lipid_evidence_status": molecule.lipid_evidence_status,
                "toxicity_evidence_status": molecule.toxicity_evidence_status,
                "toxicity_evidence_coverage": molecule.toxicity_evidence_coverage,
                "safety_clearance_confidence": molecule.safety_clearance_confidence,
                "proxy_clearance_confidence": molecule.proxy_clearance_confidence,
                "applicability": molecule.toxicity_model_applicability,
                "selection_factors": molecule.selection_factors,
                "selection_reason": molecule.selection_reason,
                "input_structure_hash": molecule.input_structure_hash,
                "epa_audit": molecule.epa_audit,
                "dili_audit": molecule.dili_audit,
                "evidence_source_audit": molecule.evidence_source_audit,
                "attribution_ids": sorted(
                    {a.evidence_id for a in molecule.attributions if a.evidence_id}
                ),
            }
            fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return out


def export_evidence_ledger_jsonl(
    molecules: list[ScoreRecord], output_path: str | Path
) -> Path:
    """Export provenance for every evidence hit and explicit missing states."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for molecule in molecules:
            hits = molecule.evidence_hits
            if not hits:
                fh.write(json.dumps({
                    "schema_version": "evidence-ledger-v2",
                    "molecule_id": molecule.molecule_id,
                    "inchikey": molecule.inchikey,
                    "input_structure_hash": molecule.input_structure_hash,
                    "evidence_id": f"audit_missing:{molecule.inchikey or molecule.molecule_id}",
                    "query_status": "not_queried",
                    "evidence_role": "query_audit",
                    "evidence_type": "query_audit",
                    "direction": "unknown",
                    "claim_ceiling": molecule.claim_ceiling,
                    "audit_missing": list(molecule.audit_missing),
                    "epa_audit": molecule.epa_audit,
                    "dili_audit": molecule.dili_audit,
                    "evidence_source_audit": molecule.evidence_source_audit,
                }, ensure_ascii=False, sort_keys=True) + "\n")
                continue
            for hit in hits:
                fh.write(json.dumps({
                    "schema_version": "evidence-ledger-v2",
                    "molecule_id": molecule.molecule_id,
                    "inchikey": molecule.inchikey,
                    "input_structure_hash": molecule.input_structure_hash,
                    "adapter_id": hit.adapter_id,
                    "query_type": hit.query_type,
                    "evidence_id": hit.evidence_id,
                    "endpoint": hit.endpoint,
                    "direction": hit.direction,
                    "evidence_role": hit.evidence_role,
                    "evidence_type": hit.evidence_type,
                    "query_status": hit.query_status,
                    "score": hit.score,
                    "confidence": hit.confidence,
                    "source_url": hit.source_url,
                    "retrieved_at": hit.retrieved_at,
                    "adapter_version": hit.adapter_version,
                    "source_version": hit.source_version,
                    "query_params": hit.query_params,
                    "response_sha256": hit.response_sha256,
                    "license": hit.license,
                    "claim_ceiling": molecule.claim_ceiling,
                    "audit_missing": list(molecule.audit_missing),
                    "epa_audit": molecule.epa_audit,
                    "dili_audit": molecule.dili_audit,
                    "evidence_source_audit": molecule.evidence_source_audit,
                }, ensure_ascii=False, sort_keys=True) + "\n")
    return out


def export_citations_jsonl(
    molecules: list[ScoreRecord], output_path: str | Path
) -> Path:
    """Export candidate-level citation rows (no fabricated PMID/DOI)."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for molecule in molecules:
            citations = molecule.citations
            if not citations:
                fh.write(
                    json.dumps(
                        {
                            "schema_version": "citations-v1",
                            "molecule_id": molecule.molecule_id,
                            "inchikey": molecule.inchikey,
                            "evidence_id": "",
                            "evidence_type": "unresolved",
                            "note": "no_citations",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
                continue
            for cite in citations:
                fh.write(
                    json.dumps(
                        {
                            "schema_version": "citations-v1",
                            "molecule_id": molecule.molecule_id,
                            "inchikey": molecule.inchikey,
                            "source": cite.source,
                            "accession": cite.accession,
                            "evidence_type": cite.evidence_type,
                            "endpoint": cite.endpoint,
                            "direction": cite.direction,
                            "value": cite.value,
                            "unit": cite.unit,
                            "assay_context": cite.assay_context,
                            "matched_entity": cite.matched_entity,
                            "pmid_or_doi": cite.pmid_or_doi,
                            "queried_at": cite.queried_at,
                            "evidence_id": cite.evidence_id,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
    return out


def export_selection_audit_jsonl(
    rows: list[dict[str, object]], output_path: str | Path
) -> Path:
    """Export structured selected / not_selected reasons for the shortlist."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            payload = {"schema_version": "selection-audit-v1", **row}
            fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return out


def export_mechanism_graph_json(
    graphs: list[MechanismGraph], output_path: str | Path
) -> Path:
    """Write the non-scoring candidate-to-context graph as a deterministic artifact."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "molmind-mechanism-graph-v1",
        "ranking_effect": "none",
        "graphs": serialize_mechanism_graphs(graphs),
    }
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out


def export_hepg2_ffa_resources_json(
    payload: dict[str, object], output_path: str | Path
) -> Path:
    """Write the public HepG2-FFA context registry as a non-scoring artifact."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out
