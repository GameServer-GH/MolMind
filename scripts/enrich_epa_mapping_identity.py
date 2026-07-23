#!/usr/bin/env python3
"""Join completed EPA identity mappings with pipeline-standardized structures.

This is a local, network-free migration for mapping checkpoints created before
``standardized_inchikey`` was recorded.  It preserves the original EPA query
result and writes a new immutable mapping artifact plus manifest.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.ingest.cache import (  # noqa: E402
    feature_cache_path,
    load_feature_cache,
    save_feature_cache,
    sha256_file,
)
from services.ingest.parser import parse_sdf_detailed  # noqa: E402
from services.pipeline.config_loader import load_config  # noqa: E402
from services.public_data.epa_ctx_bundle import utc_now  # noqa: E402


def _audit_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _load_records(sdf: Path):
    cfg = load_config(mode="offline")
    feature_cfg = cfg.feature_cache
    cache_dir = ROOT / str(feature_cfg.get("directory") or ".molmind_cache/features")
    cache_path = feature_cache_path(
        sdf,
        cache_dir=cache_dir,
        schema_version=str(feature_cfg.get("schema_version") or "ingest-features-v1"),
    )
    parsed = load_feature_cache(cache_path)
    if parsed is not None:
        return parsed, cache_path, "feature_cache"
    parsed = parse_sdf_detailed(sdf)
    if bool(feature_cfg.get("enabled", True)):
        save_feature_cache(
            cache_path,
            parsed,
            metadata={
                "source": str(sdf),
                "schema_version": str(
                    feature_cfg.get("schema_version") or "ingest-features-v1"
                ),
            },
        )
    return parsed, cache_path, "parsed"


def enrich_mapping(mapping: Path, sdf: Path, output: Path) -> dict[str, Any]:
    parsed, cache_path, identity_source = _load_records(sdf)
    by_id = {record.molecule_id: record for record in parsed.records}
    rows: list[dict[str, Any]] = []
    for line in mapping.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        record = by_id.get(str(row.get("molecule_id") or ""))
        if record is not None:
            row = {
                **row,
                "standardized_inchikey": record.inchikey,
                "standardized_smiles": record.smiles,
                "standardization_steps": list(record.standardization_steps),
                "pipeline_source_index": record.source_index,
            }
        else:
            row = {
                **row,
                "standardized_inchikey": row.get("standardized_inchikey"),
                "standardized_smiles": row.get("standardized_smiles"),
                "standardization_steps": list(row.get("standardization_steps") or []),
            }
        rows.append(row)

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    matched = sum(bool(row.get("standardized_inchikey")) for row in rows)
    changed = sum(
        bool(row.get("standardized_inchikey"))
        and row.get("standardized_inchikey") != row.get("original_inchikey")
        for row in rows
    )
    manifest = {
        "schema_version": "molmind-epa-ctx-enriched-mapping-manifest-v1",
        "captured_at": utc_now(),
        "status": "imported",
        "source_mapping": _audit_path(mapping),
        "source_mapping_sha256": sha256_file(mapping),
        "source_sdf": _audit_path(sdf),
        "source_sdf_sha256": sha256_file(sdf),
        "identity_source": identity_source,
        "feature_cache_path": _audit_path(cache_path),
        "records": len(rows),
        "standardized_identity_matches": matched,
        "original_to_standardized_changes": changed,
        "identity_fields": [
            "original_inchikey",
            "standardized_inchikey",
            "standardized_smiles",
            "standardization_steps",
        ],
        "processed_path": _audit_path(output),
        "processed_sha256": sha256_file(output),
        "missing_semantics": "audit_missing",
        "negative_search_is_negative_label": False,
    }
    output.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--sdf", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data/public/processed/epa_ctx/bulk_mapping_all_enriched.jsonl",
    )
    args = parser.parse_args()
    manifest = enrich_mapping(
        args.mapping.resolve(),
        args.sdf.resolve(),
        args.output.resolve(),
    )
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

