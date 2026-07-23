#!/usr/bin/env python3
"""Checkpointed full-catalog EPA CTX Bioactivity summary import."""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.public_data.epa_ctx_bundle import CtxClient, file_sha256, utc_now  # noqa: E402


def audit_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "data/public/processed/epa_ctx/bulk_bioactivity_summary.jsonl")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    args.mapping = args.mapping.resolve()
    args.output = args.output.resolve()
    mappings_by_dtxsid: dict[str, dict[str, Any]] = {}
    for line in args.mapping.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("dtxsid"):
            mappings_by_dtxsid.setdefault(str(row["dtxsid"]), row)
    mappings = list(mappings_by_dtxsid.values())
    existing: dict[str, dict[str, Any]] = {}
    if args.resume and args.output.is_file():
        for line in args.output.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                # Network failures are retryable audit gaps, not completed
                # evidence records. A resumed run drops and refetches them.
                if row.get("dtxsid") and row.get("status") in {
                    "returned",
                    "verified_empty",
                }:
                    existing[str(row["dtxsid"])] = row
    pending = [row for row in mappings if str(row["dtxsid"]) not in existing]
    client = CtxClient()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if existing:
        with args.output.open("w", encoding="utf-8") as handle:
            for row in existing.values():
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    results = list(existing.values())
    completed = len(existing)

    def one(mapping: dict[str, Any]) -> dict[str, Any]:
        dtxsid = str(mapping["dtxsid"])
        try:
            payload = client.get_json(f"bioactivity/data/summary/search/by-dtxsid/{dtxsid}")
            status = "returned" if payload else "verified_empty"
            error = None
        except Exception as exc:
            payload = []
            status = "network_error"
            error = {"error_type": type(exc).__name__, "error": str(exc)}
        return {
            "molecule_id": mapping.get("molecule_id"),
            "dtxsid": dtxsid,
            "source_id": "epa_ctx",
            "endpoint": "bioactivity_summary",
            "status": status,
            "record": payload,
            "retrieved_at": utc_now(),
            "error": error,
        }

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(one, mapping): mapping.get("molecule_id") for mapping in pending}
        for future in as_completed(futures):
            row = future.result()
            results.append(row)
            completed += 1
            with args.output.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            if completed % 100 == 0 or completed == len(mappings):
                print(json.dumps({
                    "progress": completed, "total": len(mappings),
                    "returned": sum(row.get("status") == "returned" for row in results),
                    "verified_empty": sum(row.get("status") == "verified_empty" for row in results),
                }), flush=True)
    results.sort(key=lambda row: row.get("molecule_id") or "")
    mapped = sum(row.get("status") == "returned" for row in results)
    manifest = {
        "schema_version": "molmind-epa-ctx-bulk-summary-manifest-v1",
        "captured_at": utc_now(),
        "status": "imported",
        "mapping_path": audit_path(args.mapping),
        "mapping_sha256": file_sha256(args.mapping),
        "mapped_dtxsid_count": len(mappings),
        "summary_returned_count": mapped,
        "summary_verified_empty_count": sum(row.get("status") == "verified_empty" for row in results),
        "network_error_count": sum(row.get("status") == "network_error" for row in results),
        "processed_path": audit_path(args.output),
        "processed_sha256": file_sha256(args.output),
        "missing_semantics": "audit_missing",
        "negative_search_is_negative_label": False,
        "next_stage_policy": "download full detail only for active/risk-relevant summaries or Top-M",
    }
    manifest_path = args.output.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
