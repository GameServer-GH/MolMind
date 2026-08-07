"""InChIKey index over public assay-grain QC tables for EvidenceFacade."""

from __future__ import annotations

from plugins.molmind_core.scientific.paths import REPO_ROOT
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from packages.models import EvidenceHit
from plugins.molmind_core.scientific.evidence_facade.bundle import infer_evidence_type

ROOT = REPO_ROOT
DEFAULT_QC_PATHS = (
    ROOT / "data/public/processed/chembl_bioactivity/records_endpoint_qc.jsonl",
    ROOT / "data/public/processed/pubchem_bioassay/records_endpoint_qc.jsonl",
    ROOT / "data/public/processed/bindingdb/records_endpoint_qc.jsonl",
    ROOT / "data/public/processed/epa_toxcast_tox21/records_endpoint_qc.jsonl",
)


@dataclass
class PublicAssayIndex:
    by_inchikey: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return sum(len(rows) for rows in self.by_inchikey.values())

    def lookup(self, inchikey: str) -> List[Dict[str, Any]]:
        key = (inchikey or "").strip()
        if not key:
            return []
        return list(self.by_inchikey.get(key, []))


def load_public_assay_index(paths: Optional[List[Path]] = None) -> PublicAssayIndex:
    index = PublicAssayIndex()
    for path in paths or list(DEFAULT_QC_PATHS):
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    inchikey = str(row.get("inchikey") or "").strip()
                    if not inchikey:
                        continue
                    index.by_inchikey.setdefault(inchikey, []).append(row)
        except OSError:
            continue
    return index


def _source_may_score(source: str, evidence_role: str) -> bool:
    """Sparse QC tables must not bias ranking except ChEMBL phenotype / ToxCast risk.

    PubChem BioAssay Active and BindingDB affinities stay annotation / mechanism
    only until candidate-level hit|verified_empty coverage exists.
    """
    src = (source or "").lower()
    if "bindingdb" in src:
        return False
    if "pubchem" in src:
        return False
    if "chembl" in src and evidence_role == "task_evidence":
        return True
    if ("toxcast" in src or "tox21" in src or "epa" in src) and evidence_role == "risk_signal":
        # EPA scoring is owned by staged CTX integration; assay-grain path zeros these.
        return False
    return False


def row_to_evidence_hit(row: Dict[str, Any]) -> EvidenceHit:
    source = str(row.get("qc_source") or row.get("source_id") or "public_assay")
    adapter_id = f"public_{source}_v1"
    evidence_role = str(row.get("evidence_role") or "annotation_only")
    direction = str(row.get("molmind_direction") or row.get("direction") or "unknown")
    query_type = "annotation"
    if evidence_role == "task_evidence" and direction == "supports":
        query_type = "lipid"
    elif evidence_role in {"task_evidence", "risk_signal"} and direction == "risk":
        query_type = "tox"
    elif evidence_role == "mechanism_support":
        query_type = "pathway"

    # Only ChEMBL phenotype task_evidence may carry non-zero lipid/tox scores here.
    # BindingDB / PubChem BioAssay stay score=0 to avoid sparse-coverage bias.
    score = 0.0
    confidence = 0.0
    query_status = "annotation_only"
    may_score = _source_may_score(source, evidence_role)
    if may_score and evidence_role == "task_evidence" and direction == "supports":
        score = 0.45
        confidence = 0.55
        query_status = "exact_hit"
    elif may_score and evidence_role == "task_evidence" and direction == "risk":
        score = 0.55
        confidence = 0.60
        query_status = "exact_hit"
    elif evidence_role == "mechanism_support":
        query_status = "exact_hit"
        query_type = "pathway"
        evidence_role = "mechanism_support"
    elif evidence_role == "risk_signal":
        # Keep visible as annotation; never safety clearance / score lift here.
        query_status = "annotation_only"
        query_type = "annotation"
        evidence_role = "annotation_only"
    elif "pubchem" in source.lower() and evidence_role == "task_evidence":
        query_status = "annotation_only"
        query_type = "annotation"
        evidence_role = "annotation_only"

    compound = str(row.get("compound_id") or "")
    assay = str(row.get("assay_id") or "")
    hit = EvidenceHit(
        adapter_id=adapter_id,
        query_type=query_type,
        score=score,
        confidence=confidence,
        evidence_id=f"{adapter_id}:{assay}:{compound}",
        payload={
            "source_id": source,
            "compound_id": compound,
            "assay_id": assay,
            "qc_tier": row.get("qc_tier"),
            "classification": row.get("classification"),
            "eligible_for_endpoint_training": row.get("eligible_for_endpoint_training"),
            "value": row.get("value"),
            "unit": row.get("unit"),
            "endpoint": row.get("endpoint"),
            "inchikey": row.get("inchikey"),
            "standardized_smiles": row.get("standardized_smiles"),
        },
        endpoint=str(row.get("endpoint") or ""),
        direction=direction,
        evidence_role=evidence_role,
        provenance_status="public_assay_grain_qc",
        source_url=str(row.get("source_url") or ""),
        retrieved_at=str(row.get("retrieved_at") or ""),
        adapter_version=f"{adapter_id}:qc-v1",
        source_version=f"{adapter_id}:qc-v1",
        query_params={"inchikey": row.get("inchikey"), "assay_id": assay},
        response_sha256="",
        license=str(row.get("license") or ""),
        query_status=query_status,  # type: ignore[arg-type]
        evidence_type=str(row.get("evidence_type") or "unresolved"),  # type: ignore[arg-type]
    )
    if hit.evidence_type == "unresolved":
        hit.evidence_type = infer_evidence_type(hit)  # type: ignore[assignment]
    return hit


def hits_for_inchikey(index: PublicAssayIndex, inchikey: str) -> List[EvidenceHit]:
    return [row_to_evidence_hit(row) for row in index.lookup(inchikey)]
