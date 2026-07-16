"""从 EvidenceHit 抽取候选级可追溯引用行。"""

from __future__ import annotations

from typing import Any

from packages.models import EvidenceCitation, EvidenceHit
from services.evidence_facade.bundle import infer_evidence_type


def _first_str(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _structured_assay_context(payload: dict[str, Any]) -> tuple[str, str, str]:
    """Return (value, unit, assay_context) from common ChEMBL/PubChem payload shapes."""
    structured = payload.get("structured_hits") or payload.get("activities") or []
    if isinstance(structured, list) and structured:
        first = structured[0] if isinstance(structured[0], dict) else {}
        value = _first_str(first, "standard_value", "value", "activity_value")
        unit = _first_str(first, "standard_units", "units", "unit")
        assay = _first_str(
            first,
            "assay_description",
            "assay_type",
            "bao_label",
            "standard_type",
        )
        return value, unit, assay
    value = _first_str(payload, "standard_value", "value")
    unit = _first_str(payload, "standard_units", "units", "unit")
    assay = _first_str(payload, "assay_description", "assay_type", "bao_label")
    return value, unit, assay


def citation_from_hit(hit: EvidenceHit) -> EvidenceCitation:
    payload = hit.payload or {}
    value, unit, assay = _structured_assay_context(payload)
    accession = _first_str(
        payload,
        "chembl_id",
        "cid",
        "accession",
        "molecule_chembl_id",
        "ensembl_id",
        "uniprot_id",
        "reactome_id",
        "mondo_id",
    )
    matched = _first_str(
        payload,
        "matched_entity",
        "target_pref_name",
        "pref_name",
        "compound_name",
    )
    pmid = _first_str(payload, "pmid", "pubmed_id", "doi", "pmid_or_doi")
    return EvidenceCitation(
        source=hit.adapter_id,
        accession=accession,
        evidence_type=infer_evidence_type(hit),
        endpoint=hit.endpoint,
        direction=hit.direction,
        value=value,
        unit=unit,
        assay_context=assay,
        matched_entity=matched,
        pmid_or_doi=pmid,
        queried_at=hit.retrieved_at,
        evidence_id=hit.evidence_id,
    )


def citations_from_hits(hits: list[EvidenceHit]) -> list[EvidenceCitation]:
    """Keep scoring-relevant and annotation hits; drop pure transport audit noise optionally.

    Query-audit rows are retained when they encode identity_review_required so the
    citation list remains an audit-complete package.
    """
    out: list[EvidenceCitation] = []
    seen: set[str] = set()
    for hit in hits:
        if hit.evidence_role == "query_audit" and hit.query_status not in {
            "identity_review_required",
            "verified_empty",
            "timeout",
            "rate_limited",
            "adapter_error",
        }:
            continue
        cite = citation_from_hit(hit)
        key = cite.evidence_id or f"{cite.source}:{cite.accession}:{cite.endpoint}"
        if key in seen:
            continue
        seen.add(key)
        out.append(cite)
    return out
