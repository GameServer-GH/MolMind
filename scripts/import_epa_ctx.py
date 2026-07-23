#!/usr/bin/env python3
"""Download Top-N EPA CTX evidence without persisting the API key."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.public_data.epa_ctx_bundle import (  # noqa: E402
    CTX_BASE, ENDPOINTS, CtxClient, file_sha256, map_candidate,
    query_candidate, response_count, utc_now,
)
from services.public_data.toxcast_ctx import normalize_toxcast_row  # noqa: E402

DEFAULT_CANDIDATES = ROOT / "data/evidence_snapshot/v2/top10_entities.json"
RAW_DIR = ROOT / "data/public/raw/epa_ctx"
PROCESSED_DIR = ROOT / "data/public/processed/epa_ctx"
MANIFEST = ROOT / "data/public/manifests/epa_ctx_top10.json"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--reuse-raw", action="store_true", help="reuse today's authenticated raw bundle and only rebuild derived artifacts")
    args = parser.parse_args()
    source = json.loads(args.candidates.read_text(encoding="utf-8"))
    candidates = list(source.get("candidates") or [])[: max(1, args.limit)]
    client = CtxClient()
    captured_at = utc_now()
    queried: list[dict[str, Any]] = []
    mappings: list[dict[str, Any]] = []
    endpoint_rows: dict[str, list[dict[str, Any]]] = {name: [] for name in ENDPOINTS}
    prior_raw = RAW_DIR / f"top10_ctx_{captured_at[:10]}.json"
    if args.reuse_raw and prior_raw.is_file():
        queried = list(json.loads(prior_raw.read_text(encoding="utf-8")).get("results") or [])
        mappings = [result["mapping"] for result in queried]
    else:
        for candidate in candidates:
            mapping = map_candidate(client, candidate)
            result = query_candidate(client, mapping)
            mappings.append(mapping)
            queried.append(result)
    for result in queried:
        mapping = result["mapping"]
        for endpoint, payload in result["responses"].items():
            rows = payload if isinstance(payload, list) else ([payload] if payload else [])
            for row in rows:
                endpoint_rows[endpoint].append({
                    "molecule_id": mapping.get("molecule_id"), "dtxsid": mapping.get("dtxsid"),
                    "source_id": "epa_ctx", "endpoint_group": endpoint,
                    "record": row, "retrieved_at": captured_at,
                })

    active_assay_rows: list[dict[str, Any]] = []
    active_aeids = sorted({
        int(item["record"]["aeid"])
        for item in endpoint_rows["bioactivity_detail"]
        if item["record"].get("aeid") is not None
        and float(item["record"].get("hitc") or 0) >= 0.9
    })
    assay_errors: list[dict[str, Any]] = []
    def fetch_assay(aeid: int) -> tuple[int, Any]:
        return aeid, client.get_json(f"bioactivity/assay/search/by-aeid/{aeid}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_assay, aeid): aeid for aeid in active_aeids}
        completed: list[tuple[int, Any]] = []
        for future in as_completed(futures):
            aeid = futures[future]
            try:
                completed.append(future.result())
            except Exception as exc:
                assay_errors.append({"aeid": aeid, "error_type": type(exc).__name__, "error": str(exc)})
    for aeid, payload in sorted(completed):
        try:
            rows = payload if isinstance(payload, list) else ([payload] if payload else [])
            for row in rows:
                active_assay_rows.append({
                    "aeid": aeid,
                    "source_id": "epa_ctx",
                    "record": row,
                    "retrieved_at": captured_at,
                })
        except Exception as exc:
            assay_errors.append({"aeid": aeid, "error_type": type(exc).__name__, "error": str(exc)})

    raw_path = RAW_DIR / f"top10_ctx_{captured_at[:10]}.json"
    write_json(raw_path, {
        "schema_version": "molmind-epa-ctx-raw-v1",
        "captured_at": captured_at,
        "candidate_source": str(args.candidates.relative_to(ROOT)),
        "candidate_source_sha256": file_sha256(args.candidates),
        "base_url": CTX_BASE,
        "authentication": "x-api-key resolved from project config/runtime; secret omitted from artifact",
        "results": queried,
        "active_assay_metadata": active_assay_rows,
        "active_assay_errors": assay_errors,
    })
    mapping_path = PROCESSED_DIR / "candidate_mapping.jsonl"
    write_jsonl(mapping_path, mappings)
    processed_paths: dict[str, str] = {"candidate_mapping": str(mapping_path.relative_to(ROOT))}
    for endpoint, rows in endpoint_rows.items():
        path = PROCESSED_DIR / f"{endpoint}.jsonl"
        write_jsonl(path, rows)
        processed_paths[endpoint] = str(path.relative_to(ROOT))
    assay_path = PROCESSED_DIR / "active_assay_metadata.jsonl"
    write_jsonl(assay_path, active_assay_rows)
    processed_paths["active_assay_metadata"] = str(assay_path.relative_to(ROOT))

    risk_rows: list[dict[str, Any]] = []
    for mapping in mappings:
        molecule_id = mapping.get("molecule_id")
        detail = [row["record"] for row in endpoint_rows["bioactivity_detail"] if row["molecule_id"] == molecule_id]
        active = [row for row in detail if float(row.get("hitc") or 0) >= 0.9]
        summaries = [row["record"] for row in endpoint_rows["bioactivity_summary"] if row["molecule_id"] == molecule_id]
        risk_rows.append({
            "molecule_id": molecule_id,
            "dtxsid": mapping.get("dtxsid"),
            "mapping_status": mapping.get("mapping_status"),
            "bioactivity_record_count": len(detail),
            "active_hit_count_hitc_gte_0_9": len(active),
            "active_aeids": sorted({row.get("aeid") for row in active if row.get("aeid") is not None}),
            "bioactivity_summary": summaries[0] if summaries else None,
            "toxval_record_count": sum(row["molecule_id"] == molecule_id for row in endpoint_rows["toxval"]),
            "toxref_summary_record_count": sum(row["molecule_id"] == molecule_id for row in endpoint_rows["toxref_summary"]),
            "interpretation": "risk_signal_present" if active else "audit_missing_or_no_active_hit; not a safety clearance",
            "ranking_effect": "risk_signal_only",
            "retrieved_at": captured_at,
        })
    risk_path = PROCESSED_DIR / "candidate_risk_summary.jsonl"
    write_jsonl(risk_path, risk_rows)
    processed_paths["candidate_risk_summary"] = str(risk_path.relative_to(ROOT))

    assay_by_aeid = {row["aeid"]: row["record"] for row in active_assay_rows}
    mapping_by_dtxsid = {row.get("dtxsid"): row for row in mappings if row.get("dtxsid")}
    normalized_toxcast: list[dict[str, Any]] = []
    for item in endpoint_rows["bioactivity_detail"]:
        record = dict(item["record"])
        identity = mapping_by_dtxsid.get(item.get("dtxsid"), {})
        record.setdefault("smiles", identity.get("ctx_smiles"))
        record.setdefault("preferredName", identity.get("preferred_name"))
        record.setdefault("casrn", identity.get("casrn"))
        record.update(assay_by_aeid.get(record.get("aeid"), {}))
        normalized = normalize_toxcast_row(
            record,
            source_id="epa_toxcast_tox21",
            license_policy="preserve_version_and_endpoint_provenance",
            api_base=f"{CTX_BASE}/bioactivity",
            retrieved_at=captured_at,
        )
        normalized["molecule_id"] = item["molecule_id"]
        normalized_toxcast.append(normalized)
    toxcast_path = ROOT / "data/public/processed/epa_toxcast_tox21/records.jsonl"
    write_jsonl(toxcast_path, normalized_toxcast)
    processed_paths["normalized_toxcast_assay_grain"] = str(toxcast_path.relative_to(ROOT))

    verified_empty = {
        result["mapping"].get("molecule_id"): [
            name for name in ENDPOINTS
            if name in result["responses"] and response_count(result["responses"][name]) == 0
        ] for result in queried
    }
    mapped = sum(bool(row.get("dtxsid")) for row in mappings)
    exact = sum(row.get("mapping_status") == "exact_identifier_match" for row in mappings)
    manifest = {
        "schema_version": "molmind-epa-ctx-import-manifest-v1",
        "captured_at": captured_at,
        "status": "imported" if mapped else "audit_missing",
        "source": "US EPA CTX APIs",
        "source_url": "https://www.epa.gov/comptox-tools/computational-toxicology-and-exposure-apis",
        "api_base": CTX_BASE,
        "api_key_persisted": False,
        "candidate_count": len(candidates),
        "mapped_dtxsid_count": mapped,
        "exact_original_inchikey_match_count": exact,
        "endpoint_record_counts": {name: len(rows) for name, rows in endpoint_rows.items()},
        "active_assay_metadata_count": len(active_assay_rows),
        "normalized_toxcast_assay_grain_count": len(normalized_toxcast),
        "active_assay_metadata_errors": assay_errors,
        "verified_empty_endpoints": verified_empty,
        "missing_semantics": "audit_missing",
        "negative_search_is_negative_label": False,
        "empty_endpoint_interpretation": "empty endpoint is not low toxicity",
        "ranking_effect": "risk_signal_only",
        "raw_path": str(raw_path.relative_to(ROOT)),
        "raw_sha256": file_sha256(raw_path),
        "processed_paths": processed_paths,
        "processed_sha256": {name: file_sha256(ROOT / path) for name, path in processed_paths.items()},
        "errors": [item for result in queried for item in result["errors"]],
    }
    write_json(MANIFEST, manifest)
    print(json.dumps({
        "status": manifest["status"], "candidate_count": len(candidates),
        "mapped": mapped, "exact": exact,
        "endpoint_record_counts": manifest["endpoint_record_counts"],
        "manifest": str(MANIFEST.relative_to(ROOT)),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
