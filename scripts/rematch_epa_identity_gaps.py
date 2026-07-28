#!/usr/bin/env python3
"""Rematch EPA CTX identity gaps and refresh bioactivity summaries.

Default scope:
  - ``audit_missing`` (no DTXSID)
  - ``identifier_match_requires_structure_audit`` (usually CAS-only)

Fast path: rematch gap rows in-process from the existing enriched mapping
(no full-SDF tautomer re-scan). Then enrich standardized fields, resume
bioactivity summary for newly mapped DTXSIDs, and optionally bake evidence.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.public_data.epa_ctx_bundle import (  # noqa: E402
    CtxClient,
    file_sha256,
    map_candidate,
    utc_now,
)

DEFAULT_SDF = ROOT / "data/T001 TargetMol现货产品22966.sdf"
DEFAULT_MAP = ROOT / "data/public/processed/epa_ctx/bulk_mapping_all_enriched.jsonl"
DEFAULT_MAP_RAW = ROOT / "data/public/processed/epa_ctx/bulk_mapping_all.jsonl"
DEFAULT_BIO = ROOT / "data/public/processed/epa_ctx/bulk_bioactivity_summary.jsonl"
DEFAULT_CACHE = ROOT / "data/public/cache/evidence_query_state.sqlite"
GAP_STATUSES = {
    "audit_missing",
    "identifier_match_requires_structure_audit",
}


def _stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y%m%d_%H%M%S")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(str(r.get("mapping_status") or "?") for r in rows))


def _bio_counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    status = Counter(str(r.get("status") or "?") for r in rows)
    nonempty = 0
    for row in rows:
        rec = row.get("record")
        if (isinstance(rec, list) and rec) or (isinstance(rec, dict) and rec):
            nonempty += 1
    return {"status": dict(status), "nonempty_record": nonempty, "total": len(rows)}


def _entity_keys(row: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for field in (
        "original_inchikey",
        "standardized_inchikey",
        "cas",
        "molecule_id",
        "id",
        "mapping_value",
    ):
        value = row.get(field)
        if value:
            keys.add(str(value))
    return keys


def _clear_identity_cache(cache_db: Path, keys: set[str]) -> int:
    if not cache_db.is_file() or not keys:
        return 0
    con = sqlite3.connect(cache_db)
    try:
        tables = {
            str(r[0])
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "source_query" not in tables:
            return 0
        deleted = 0
        batch = list(keys)
        for i in range(0, len(batch), 500):
            chunk = batch[i : i + 500]
            placeholders = ",".join("?" for _ in chunk)
            cur = con.execute(
                f"""
                DELETE FROM source_query
                WHERE source_id='epa_ctx'
                  AND endpoint='identity_lookup'
                  AND entity_key IN ({placeholders})
                  AND status IN ('verified_empty','query_failed','auth_missing')
                """,
                chunk,
            )
            deleted += int(cur.rowcount or 0)
        con.commit()
        return deleted
    finally:
        con.close()


def _run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def _write_mapping_manifest(mapping_path: Path, *, source_note: str) -> dict[str, Any]:
    """Keep bulk mapping loadable by EPAContextIndex (_stable_bulk_artifact)."""
    rows = _load_jsonl(mapping_path)
    mapped = [row for row in rows if row.get("dtxsid")]
    manifest = {
        "schema_version": "molmind-epa-ctx-enriched-mapping-manifest-v1",
        "captured_at": utc_now(),
        "status": "imported",
        "source_note": source_note,
        "records": len(rows),
        "dtxsid_mapped": len(mapped),
        "exact_identifier_match": sum(
            row.get("mapping_status") == "exact_identifier_match" for row in mapped
        ),
        "identity_fields": [
            "original_inchikey",
            "standardized_inchikey",
            "standardized_smiles",
            "standardization_steps",
        ],
        "processed_path": str(mapping_path.relative_to(ROOT))
        if mapping_path.is_relative_to(ROOT)
        else str(mapping_path),
        "processed_sha256": file_sha256(mapping_path),
        "missing_semantics": "audit_missing",
        "negative_search_is_negative_label": False,
    }
    mapping_path.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _row_key(row: dict[str, Any]) -> str:
    if row.get("source_index") is not None:
        return f"idx:{int(row['source_index'])}"
    mid = row.get("molecule_id") or row.get("id")
    if mid:
        return f"id:{mid}"
    return f"ik:{row.get('original_inchikey') or row.get('standardized_inchikey') or ''}"


def _rematch_gaps(
    gap_rows: list[dict[str, Any]],
    *,
    workers: int,
) -> list[dict[str, Any]]:
    client = CtxClient()
    out: list[dict[str, Any]] = []
    total = len(gap_rows)

    def one(row: dict[str, Any]) -> dict[str, Any]:
        candidate = {
            "molecule_id": row.get("molecule_id") or row.get("id"),
            "original_inchikey": row.get("original_inchikey"),
            "standardized_inchikey": row.get("standardized_inchikey"),
            "cas": row.get("cas") or row.get("casrn"),
        }
        try:
            mapped = map_candidate(client, candidate)
        except Exception as exc:  # noqa: BLE001
            mapped = {
                "molecule_id": candidate.get("molecule_id"),
                "dtxsid": None,
                "mapping_status": "network_error",
                "mapping_basis": None,
                "errors": [{"error_type": type(exc).__name__, "error": str(exc)}],
                "retrieved_at": utc_now(),
            }
        merged = dict(row)
        # Preserve structure fields; overwrite mapping outcome.
        for key, value in mapped.items():
            merged[key] = value
        merged["rematch_at"] = utc_now()
        return merged

    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(one, row): row for row in gap_rows}
        for future in as_completed(futures):
            out.append(future.result())
            completed += 1
            if completed % 50 == 0 or completed == total:
                mapped_n = sum(1 for row in out if row.get("dtxsid"))
                exact_n = sum(
                    1 for row in out if row.get("mapping_status") == "exact_identifier_match"
                )
                print(
                    json.dumps(
                        {
                            "progress": completed,
                            "total": total,
                            "mapped": mapped_n,
                            "exact": exact_n,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdf", type=Path, default=DEFAULT_SDF)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAP)
    parser.add_argument("--mapping-raw", type=Path, default=DEFAULT_MAP_RAW)
    parser.add_argument("--bioactivity", type=Path, default=DEFAULT_BIO)
    parser.add_argument("--cache-db", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument(
        "--only-audit-missing",
        action="store_true",
        help="Only rematch audit_missing (skip CAS-only structure-audit rows)",
    )
    parser.add_argument(
        "--max-gaps",
        type=int,
        default=0,
        help="Optional cap on gap rows (0 = all)",
    )
    parser.add_argument(
        "--skip-rematch",
        action="store_true",
        help="Skip CTX rematch; only enrich + summary from current mapping",
    )
    parser.add_argument(
        "--skip-summary",
        action="store_true",
        help="Skip bioactivity summary refresh",
    )
    parser.add_argument(
        "--skip-enrich",
        action="store_true",
        help="Skip SDF enrich step (keeps standardized fields already on rows)",
    )
    parser.add_argument(
        "--bake",
        action="store_true",
        help="After rematch, bake Top-M evidence with --allow-live",
    )
    parser.add_argument("--bake-top-m", type=int, default=120)
    args = parser.parse_args()

    stamp = _stamp()
    workdir = ROOT / "data/public/processed/epa_ctx" / f"rematch_{stamp}"
    workdir.mkdir(parents=True, exist_ok=True)
    rematch_path = workdir / "bulk_mapping_rematch.jsonl"
    report_path = workdir / "report.json"

    if not args.mapping.is_file():
        raise SystemExit(f"Mapping not found: {args.mapping}")

    before_rows = _load_jsonl(args.mapping)
    before_status = _status_counts(before_rows)
    before_bio = _bio_counts(_load_jsonl(args.bioactivity))
    print("before_mapping", before_status, flush=True)
    print("before_bio", before_bio, flush=True)

    shutil.copy2(args.mapping, workdir / "bulk_mapping_all_enriched.jsonl.bak")
    if args.bioactivity.is_file():
        shutil.copy2(args.bioactivity, workdir / "bulk_bioactivity_summary.jsonl.bak")

    gap_statuses = {"audit_missing"} if args.only_audit_missing else GAP_STATUSES
    keep_rows: list[dict[str, Any]] = []
    gap_rows: list[dict[str, Any]] = []
    for row in before_rows:
        status = str(row.get("mapping_status") or "")
        is_gap = status in gap_statuses or not row.get("dtxsid")
        if is_gap:
            gap_rows.append(row)
        else:
            keep_rows.append(row)
    if args.max_gaps and args.max_gaps > 0:
        gap_rows = gap_rows[: args.max_gaps]

    missing_ids = [
        str(r.get("molecule_id") or r.get("id") or "")
        for r in gap_rows
        if (r.get("molecule_id") or r.get("id"))
    ]
    (workdir / "gap_molecule_ids.txt").write_text(
        "\n".join(missing_ids) + ("\n" if missing_ids else ""),
        encoding="utf-8",
    )
    print(
        {
            "workdir": str(workdir),
            "kept": len(keep_rows),
            "gaps_to_rematch": len(gap_rows),
            "gap_statuses": sorted(gap_statuses),
        },
        flush=True,
    )

    cache_keys: set[str] = set()
    for row in gap_rows:
        cache_keys |= _entity_keys(row)
    deleted = _clear_identity_cache(args.cache_db, cache_keys)
    print({"cache_keys": len(cache_keys), "deleted_cache_rows": deleted}, flush=True)

    if args.skip_rematch:
        rematched_gaps = gap_rows
    else:
        rematched_gaps = _rematch_gaps(gap_rows, workers=args.workers)

    by_key = {_row_key(row): row for row in keep_rows}
    for row in rematched_gaps:
        by_key[_row_key(row)] = row
    # Preserve original catalog order when possible.
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in before_rows:
        key = _row_key(row)
        chosen = by_key.get(key, row)
        merged.append(chosen)
        seen.add(key)
    for key, row in by_key.items():
        if key not in seen:
            merged.append(row)

    _write_jsonl(rematch_path, merged)

    py = sys.executable
    final_mapping = rematch_path
    if not args.skip_enrich:
        if not args.sdf.is_file():
            raise SystemExit(f"SDF not found for enrich: {args.sdf}")
        enriched_tmp = workdir / "bulk_mapping_all_enriched.next.jsonl"
        _run(
            [
                py,
                str(ROOT / "scripts/enrich_epa_mapping_identity.py"),
                "--mapping",
                str(rematch_path),
                "--sdf",
                str(args.sdf),
                "--output",
                str(enriched_tmp),
            ]
        )
        final_mapping = enriched_tmp

    shutil.copy2(final_mapping, args.mapping)
    shutil.copy2(rematch_path, args.mapping_raw)
    mapping_manifest = _write_mapping_manifest(
        args.mapping,
        source_note=f"rematch_epa_identity_gaps:{stamp}",
    )
    print({"mapping_manifest": mapping_manifest.get("processed_sha256")}, flush=True)

    if not args.skip_summary:
        _run(
            [
                py,
                str(ROOT / "scripts/import_epa_ctx_bulk_summary.py"),
                "--mapping",
                str(args.mapping),
                "--output",
                str(args.bioactivity),
                "--workers",
                str(max(1, args.workers)),
                "--resume",
            ]
        )

    after_rows = _load_jsonl(args.mapping)
    after_status = _status_counts(after_rows)
    after_bio = _bio_counts(_load_jsonl(args.bioactivity))
    before_dtx = {str(r["dtxsid"]) for r in before_rows if r.get("dtxsid")}
    after_dtx = {str(r["dtxsid"]) for r in after_rows if r.get("dtxsid")}
    new_dtx = sorted(after_dtx - before_dtx)

    report = {
        "captured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "workdir": str(workdir.relative_to(ROOT)),
        "gaps_requested": len(gap_rows),
        "before_mapping_status": before_status,
        "after_mapping_status": after_status,
        "before_bio": before_bio,
        "after_bio": after_bio,
        "new_dtxsid_count": len(new_dtx),
        "new_dtxsids_sample": new_dtx[:20],
        "exact_gain": int(after_status.get("exact_identifier_match", 0))
        - int(before_status.get("exact_identifier_match", 0)),
        "audit_missing_delta": int(after_status.get("audit_missing", 0))
        - int(before_status.get("audit_missing", 0)),
        "cas_audit_delta": int(
            after_status.get("identifier_match_requires_structure_audit", 0)
        )
        - int(before_status.get("identifier_match_requires_structure_audit", 0)),
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)

    if args.bake:
        if not args.sdf.is_file():
            raise SystemExit(f"SDF not found for bake: {args.sdf}")
        _run(
            [
                py,
                "-m",
                "apps.cli.main",
                "--input",
                str(args.sdf),
                "--bake-evidence",
                "--bake-top-m",
                str(max(1, args.bake_top_m)),
                "--allow-live",
            ]
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
