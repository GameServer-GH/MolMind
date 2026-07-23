#!/usr/bin/env python3
"""Checkpointed SDF→EPA CTX DTXSID mapping pilot/full planner.

This stage intentionally maps identities only. It does not download all
ToxCast concentration-response rows. Use the resulting DTXSID list for a
second, filtered summary/detail stage.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rdkit import Chem  # noqa: E402
from services.evidence_gateway import EvidenceQueryCache  # noqa: E402
from services.ingest.parser import _standardize_mol  # noqa: E402
from services.public_data.epa_ctx_bundle import CtxClient, file_sha256, map_candidate, utc_now  # noqa: E402


def audit_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def read_sdf(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, mol in enumerate(Chem.SDMolSupplier(str(path), sanitize=False, removeHs=False)):
        if limit is not None and len(rows) >= limit:
            break
        if mol is None:
            rows.append({
                "source_index": index,
                "molecule_id": f"SDF:{index}",
                "mapping_status": "invalid_sdf_record",
            })
            continue
        row: dict[str, Any] = {"source_index": index}
        for key in ("ID", "CAS", "Formula", "MolWt"):
            if mol.HasProp(key):
                try:
                    row[key.lower()] = mol.GetProp(key)
                except UnicodeDecodeError:
                    # Product SDF contains a few non-UTF8 annotations; IDs/CAS
                    # are ASCII and remain readable while opaque fields are
                    # not needed for CTX mapping.
                    try:
                        row[key.lower()] = mol.GetProp(key).encode("latin-1", errors="ignore").decode("utf-8", errors="ignore")
                    except Exception:
                        row[key.lower()] = None
        try:
            original = Chem.Mol(mol)
            Chem.SanitizeMol(original)
            row["original_inchikey"] = Chem.MolToInchiKey(original)
            standardized, steps = _standardize_mol(original)
            row["standardized_smiles"] = Chem.MolToSmiles(
                standardized, isomericSmiles=True
            )
            row["standardized_inchikey"] = Chem.MolToInchiKey(standardized)
            row["standardization_steps"] = list(steps)
        except Exception:
            row["original_inchikey"] = None
            row["standardized_smiles"] = None
            row["standardized_inchikey"] = None
            row["standardization_steps"] = []
        row["molecule_id"] = row.get("id") or f"SDF:{index}"
        rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdf", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--plan-only", action="store_true", help="scan the SDF without calling EPA")
    parser.add_argument("--offline", action="store_true", help="use only local query state; never call EPA")
    parser.add_argument("--resume", action="store_true", help="reuse completed JSONL rows and skip their source indexes")
    parser.add_argument("--output", type=Path, default=ROOT / "data/public/processed/epa_ctx/bulk_mapping_pilot.jsonl")
    args = parser.parse_args()
    args.sdf = args.sdf.resolve()
    args.output = args.output.resolve()
    candidates = read_sdf(args.sdf, max(1, args.limit))
    if args.plan_only:
        cas = [row.get("cas") for row in candidates if row.get("cas")]
        inchikeys = [row.get("original_inchikey") for row in candidates if row.get("original_inchikey")]
        print(json.dumps({
            "schema_version": "molmind-epa-ctx-bulk-plan-v1",
            "captured_at": utc_now(),
            "source_sdf": audit_path(args.sdf),
            "source_sdf_sha256": file_sha256(args.sdf),
            "records_scanned": len(candidates),
            "cas_present": len(cas),
            "unique_cas": len(set(cas)),
            "inchikey_present": len(inchikeys),
            "unique_inchikey": len(set(inchikeys)),
            "next_stage_policy": "map identities first; summaries next; detail only for active/risk-relevant summaries",
            "missing_semantics": "audit_missing",
            "negative_search_is_negative_label": False,
        }, ensure_ascii=False))
        return 0
    client = None if args.offline else CtxClient()
    cache = EvidenceQueryCache(ROOT / "data/public/cache/evidence_query_state.sqlite")

    def entity_cache_key(candidate: dict[str, Any]) -> str:
        return str(
            candidate.get("original_inchikey")
            or candidate.get("standardized_inchikey")
            or candidate.get("cas")
            or candidate.get("molecule_id")
        )

    def record_cache_result(candidate: dict[str, Any], row: dict[str, Any]) -> None:
        if row.get("mapping_status") == "invalid_sdf_record":
            return
        entity_key = entity_cache_key(candidate)
        mapping_payload = {
            key: value
            for key, value in row.items()
            if key not in {"source_index", "molecule_id", "source_sdf", "id", "cas", "formula", "molwt"}
        }
        if row.get("dtxsid"):
            cache.record(
                source_id="epa_ctx",
                entity_key=entity_key,
                endpoint="identity_lookup",
                status="hit",
                ttl=timedelta(days=90),
                payload=mapping_payload,
                source_version="ctx-api-live",
            )
        elif row.get("mapping_status") == "network_error" or row.get("errors"):
            cache.record(
                source_id="epa_ctx",
                entity_key=entity_key,
                endpoint="identity_lookup",
                status="query_failed",
                retry_after=timedelta(hours=1),
                error=RuntimeError(str(row.get("error") or row.get("errors"))),
                source_version="ctx-api-live",
            )
        else:
            cache.record(
                source_id="epa_ctx",
                entity_key=entity_key,
                endpoint="identity_lookup",
                status="verified_empty",
                ttl=timedelta(days=14),
                source_version="ctx-api-live",
            )

    def one(candidate: dict[str, Any]) -> dict[str, Any]:
        assert client is not None
        mapped = map_candidate(client, candidate)
        return {**candidate, **mapped, "source_sdf": audit_path(args.sdf), "retrieved_at": utc_now()}

    existing: dict[int, dict[str, Any]] = {}
    if args.resume and args.output.is_file():
        for line in args.output.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("source_index") is not None:
                existing[int(row["source_index"])] = row
                entity_key = entity_cache_key(row)
                if entity_key and cache.decide(
                    source_id="epa_ctx",
                    entity_key=entity_key,
                    endpoint="identity_lookup",
                    online=False,
                ).status == "not_queried":
                    cache.upsert_entity(
                        entity_key,
                        original_inchikey=row.get("original_inchikey"),
                        cas=row.get("cas"),
                    )
                    record_cache_result(row, row)
    pending: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = list(existing.values())
    for candidate in candidates:
        if int(candidate.get("source_index", -1)) in existing:
            continue
        entity_key = entity_cache_key(candidate)
        cache.upsert_entity(
            entity_key,
            original_inchikey=candidate.get("original_inchikey"),
            cas=candidate.get("cas"),
        )
        decision = cache.decide(
            source_id="epa_ctx",
            entity_key=entity_key,
            endpoint="identity_lookup",
            online=not args.offline,
        )
        if decision.action == "local_hit":
            try:
                payload = cache.load_payload(
                    source_id="epa_ctx",
                    entity_key=entity_key,
                    endpoint="identity_lookup",
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict) and payload.get("dtxsid"):
                results.append({
                    **candidate,
                    **payload,
                    "molecule_id": candidate.get("molecule_id"),
                    "source_sdf": audit_path(args.sdf),
                    "cache_action": "local_hit",
                })
                continue
        if decision.action == "skip_fresh_verified_empty":
            results.append({
                **candidate,
                "dtxsid": None,
                "mapping_status": "audit_missing",
                "source_sdf": audit_path(args.sdf),
                "cache_action": "skip_fresh_verified_empty",
                "missing_semantics": "audit_missing",
            })
            continue
        if decision.action == "offline_missing":
            results.append({
                **candidate,
                "dtxsid": None,
                "mapping_status": "audit_missing",
                "source_sdf": audit_path(args.sdf),
                "cache_action": "offline_missing",
                "missing_semantics": "audit_missing",
            })
            continue
        pending.append(candidate)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if results:
        with args.output.open("w", encoding="utf-8") as handle:
            for row in sorted(results, key=lambda item: item.get("source_index", 10**12)):
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    completed = len(results)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(one, candidate): candidate for candidate in pending}
        for future in as_completed(futures):
            candidate = futures[future]
            try:
                row = future.result()
            except Exception as exc:
                row = {
                    **candidate,
                    "mapping_status": "network_error",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            results.append(row)
            completed += 1
            record_cache_result(candidate, row)
            with args.output.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            if completed % 25 == 0 or completed == len(candidates):
                print(json.dumps({"progress": completed, "total": len(candidates), "mapped": sum(bool(item.get("dtxsid")) for item in results)}, ensure_ascii=False), flush=True)
    results.sort(key=lambda row: row.get("source_index", 10**12))

    mapped = [row for row in results if row.get("dtxsid")]
    manifest = {
        "schema_version": "molmind-epa-ctx-bulk-mapping-manifest-v1",
        "captured_at": utc_now(),
        "status": "imported" if mapped else "audit_missing",
        "source_sdf": audit_path(args.sdf),
        "source_sdf_sha256": file_sha256(args.sdf),
        "requested_limit": args.limit,
        "records_scanned": len(results),
        "dtxsid_mapped": len(mapped),
        "exact_original_inchikey_matches": sum(row.get("mapping_status") == "exact_identifier_match" for row in mapped),
        "cas_present": sum(bool(row.get("cas")) for row in results),
        "invalid_or_unreadable_records": sum(row.get("mapping_status") == "invalid_sdf_record" for row in results),
        "network_or_mapping_errors": sum(row.get("mapping_status") == "network_error" for row in results),
        "processed_path": audit_path(args.output),
        "processed_sha256": file_sha256(args.output),
        "identity_fields": [
            "original_inchikey",
            "standardized_inchikey",
            "standardized_smiles",
            "standardization_steps",
        ],
        "next_stage_policy": "download summaries for mapped DTXSIDs; fetch full detail only for active/risk-relevant summaries",
        "missing_semantics": "audit_missing",
        "negative_search_is_negative_label": False,
    }
    manifest_path = args.output.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cache.close()
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
