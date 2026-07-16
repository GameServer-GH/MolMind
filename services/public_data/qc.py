"""Assay-grain QC: drop Unspecified / incomplete rows; never invent negatives."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from rdkit import Chem

ROOT = Path(__file__).resolve().parents[2]
PROCESSED = ROOT / "data" / "public" / "processed"
MANIFESTS = ROOT / "data" / "public" / "manifests"

# PubChem outcomes that may enter the endpoint-QC table. Unspecified is excluded.
PUBCHEM_KEEP_OUTCOMES = frozenset({"Active", "Inactive"})
CHEMBL_ENDPOINT_CLASSES = frozenset({"positive_phenotype", "adverse_phenotype"})
BINDINGDB_KEEP_ENDPOINTS = frozenset({"Ki", "IC50", "Kd", "EC50", "Ki/IC50"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_inchikey(row: Dict[str, Any]) -> Optional[str]:
    existing = str(row.get("inchikey") or "").strip()
    if existing:
        return existing
    smiles = str(row.get("standardized_smiles") or row.get("isomeric_smiles") or "").strip()
    if not smiles:
        return None
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    return Chem.MolToInchiKey(mol) or None


def qc_pubchem_row(row: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """Return (pass, reason, normalized_row).

    Active/Inactive with resolved identity enter the QC table. Unspecified never
    becomes a negative label; it is simply excluded from endpoint-QC.
    """
    outcome = str(row.get("direction") or "").strip()
    if outcome not in PUBCHEM_KEEP_OUTCOMES:
        return False, f"outcome_excluded:{outcome or 'missing'}", {}
    inchikey = ensure_inchikey(row)
    if not inchikey:
        return False, "identity_unresolved", {}
    if not row.get("compound_id"):
        return False, "compound_id_missing", {}
    if not row.get("assay_id"):
        return False, "assay_id_missing", {}
    endpoint = str(row.get("endpoint") or "")
    has_numeric = row.get("value") is not None
    # PubChem "Active" ≠ lipid-lowering phenotype. Keep for audit/training triage
    # but map scoring role to annotation_only until phenotype direction is known.
    normalized = dict(row)
    normalized["inchikey"] = inchikey
    normalized["qc_pass"] = True
    normalized["qc_source"] = "pubchem_bioassay"
    normalized["qc_tier"] = "numeric_active" if has_numeric and outcome == "Active" else "outcome_only"
    normalized["molmind_direction"] = "unknown"
    normalized["evidence_role"] = "annotation_only"
    normalized["evidence_type"] = "identity_annotation"
    normalized["classification"] = "assay_outcome"
    normalized["eligible_for_endpoint_training"] = bool(
        outcome == "Active" and has_numeric and endpoint.upper() in {"IC50", "EC50", "CC50", "AC50"}
    )
    normalized["qc_reason"] = "pass"
    return True, "pass", normalized


def qc_chembl_row(row: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    classification = str(row.get("classification") or "annotation")
    if classification not in CHEMBL_ENDPOINT_CLASSES:
        return False, f"classification_excluded:{classification}", {}
    inchikey = ensure_inchikey(row)
    if not inchikey:
        return False, "identity_unresolved", {}
    if not row.get("compound_id") or not row.get("assay_id"):
        return False, "identity_fields_missing", {}
    normalized = dict(row)
    normalized["inchikey"] = inchikey
    normalized["qc_pass"] = True
    normalized["qc_source"] = "chembl_bioactivity"
    if classification == "positive_phenotype":
        normalized["qc_tier"] = "lipid_phenotype"
        normalized["molmind_direction"] = "supports"
        normalized["evidence_role"] = "task_evidence"
        normalized["evidence_type"] = "endpoint_evidence"
        normalized["eligible_for_endpoint_training"] = True
    else:
        normalized["qc_tier"] = "adverse_lipid_phenotype"
        normalized["molmind_direction"] = "risk"
        normalized["evidence_role"] = "task_evidence"
        normalized["evidence_type"] = "endpoint_evidence"
        normalized["eligible_for_endpoint_training"] = True
    normalized["qc_reason"] = "pass"
    normalized["classification"] = classification
    return True, "pass", normalized


def qc_bindingdb_row(row: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """BindingDB affinities are mechanism_support only — never lipid/tox labels."""
    endpoint = str(row.get("endpoint") or "").strip()
    if endpoint and endpoint not in BINDINGDB_KEEP_ENDPOINTS:
        # Keep unknown affinity types as mechanism if numeric value exists.
        if row.get("value") is None:
            return False, f"endpoint_excluded:{endpoint or 'missing'}", {}
    inchikey = ensure_inchikey(row)
    if not inchikey:
        return False, "identity_unresolved", {}
    if not row.get("compound_id") or not row.get("assay_id"):
        return False, "identity_fields_missing", {}
    if not row.get("uniprot") and not row.get("target_label"):
        return False, "target_missing", {}
    if row.get("value") is None:
        return False, "affinity_missing", {}
    normalized = dict(row)
    normalized["inchikey"] = inchikey
    normalized["qc_pass"] = True
    normalized["qc_source"] = "bindingdb"
    normalized["qc_tier"] = "binding_affinity"
    normalized["molmind_direction"] = "unknown"
    normalized["evidence_role"] = "mechanism_support"
    normalized["evidence_type"] = "mechanism_association"
    normalized["classification"] = "mechanism"
    normalized["eligible_for_endpoint_training"] = False
    normalized["qc_reason"] = "pass"
    return True, "pass", normalized


def qc_toxcast_row(row: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """Keep active ToxCast hits as risk_signal; inactive never means safe."""
    if not bool(row.get("active_hit")) and str(row.get("classification") or "") != "active_risk":
        hitc = row.get("hitc")
        try:
            active = hitc is not None and float(hitc) >= 0.9
        except (TypeError, ValueError):
            active = False
        if not active:
            return False, "inactive_excluded_not_safety_label", {}
    inchikey = ensure_inchikey(row)
    if not inchikey:
        return False, "identity_unresolved", {}
    if not row.get("compound_id") or not row.get("assay_id"):
        return False, "identity_fields_missing", {}
    normalized = dict(row)
    normalized["inchikey"] = inchikey
    normalized["qc_pass"] = True
    normalized["qc_source"] = "epa_toxcast_tox21"
    normalized["qc_tier"] = "toxcast_active_hit"
    normalized["molmind_direction"] = "risk"
    normalized["evidence_role"] = "risk_signal"
    normalized["evidence_type"] = "endpoint_evidence"
    normalized["classification"] = "active_risk"
    normalized["eligible_for_endpoint_training"] = False
    normalized["qc_reason"] = "pass"
    return True, "pass", normalized


def filter_records(
    rows: Iterable[Dict[str, Any]],
    *,
    source: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    kept: List[Dict[str, Any]] = []
    reasons: Counter = Counter()
    if source == "pubchem_bioassay":
        qc_fn = qc_pubchem_row
    elif source == "bindingdb":
        qc_fn = qc_bindingdb_row
    elif source == "epa_toxcast_tox21":
        qc_fn = qc_toxcast_row
    else:
        qc_fn = qc_chembl_row
    for row in rows:
        ok, reason, normalized = qc_fn(row)
        reasons[reason] += 1
        if ok:
            kept.append(normalized)
    return kept, dict(reasons)


def write_qc_jsonl(rows: List[Dict[str, Any]], path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    import hashlib

    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def run_assay_grain_qc(
    *,
    pubchem_path: Optional[Path] = None,
    chembl_path: Optional[Path] = None,
    bindingdb_path: Optional[Path] = None,
    toxcast_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Filter processed imports into endpoint-QC tables + audit manifest."""
    pubchem_path = pubchem_path or PROCESSED / "pubchem_bioassay" / "records.jsonl"
    chembl_path = chembl_path or PROCESSED / "chembl_bioactivity" / "records.jsonl"
    bindingdb_path = bindingdb_path or PROCESSED / "bindingdb" / "records.jsonl"
    toxcast_path = toxcast_path or PROCESSED / "epa_toxcast_tox21" / "records.jsonl"
    report: Dict[str, Any] = {
        "schema_version": "molmind-public-assay-qc-v1",
        "captured_at": _utc_now(),
        "status": "qc_complete",
        "missing_semantics": "audit_missing",
        "negative_search_is_negative_label": False,
        "sources": {},
    }

    for source_id, path, out_name in (
        ("pubchem_bioassay", pubchem_path, "records_endpoint_qc.jsonl"),
        ("chembl_bioactivity", chembl_path, "records_endpoint_qc.jsonl"),
        ("bindingdb", bindingdb_path, "records_endpoint_qc.jsonl"),
        ("epa_toxcast_tox21", toxcast_path, "records_endpoint_qc.jsonl"),
    ):
        if not path.is_file():
            report["sources"][source_id] = {
                "status": "audit_missing",
                "warning": f"missing processed file: {path}",
            }
            continue
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        kept, reasons = filter_records(rows, source=source_id)
        out_path = path.parent / out_name
        sha = write_qc_jsonl(kept, out_path)
        training_n = sum(1 for row in kept if row.get("eligible_for_endpoint_training"))
        note = (
            "Unspecified/incomplete rows excluded; failures are not negative labels. "
            "PubChem Active is identity/assay outcome, not proven lipid-lowering."
        )
        if source_id == "bindingdb":
            note = (
                "BindingDB affinities are mechanism_support only; binding ≠ "
                "cellular lipid-lowering. Failures are not negative labels."
            )
        elif source_id == "epa_toxcast_tox21":
            note = (
                "ToxCast/CTX active hits are risk_signal only; inactive/non-hit is "
                "never a safety clearance. Layer separately from DILIrank."
            )
        report["sources"][source_id] = {
            "status": "qc_complete",
            "input_rows": len(rows),
            "qc_pass_rows": len(kept),
            "training_eligible_rows": training_n,
            "exclude_reasons": reasons,
            "processed_path": str(out_path.relative_to(ROOT)),
            "processed_sha256": sha,
            "note": note,
        }

    MANIFESTS.mkdir(parents=True, exist_ok=True)
    manifest_path = MANIFESTS / "assay_grain_qc.json"
    manifest_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report["manifest_path"] = str(manifest_path.relative_to(ROOT))
    return report
