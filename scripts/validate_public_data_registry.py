#!/usr/bin/env python3
"""Validate public-data provenance and ranking boundaries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "data" / "public" / "registry.yaml"
ALLOWED_ROLES = {
    "candidate_activity",
    "candidate_activity_or_cytotoxicity",
    "mechanism_support",
    "toxicity_risk",
    "clinical_liver_risk",
    "regulatory_toxicology",
    "mechanism_support_or_assay_qc",
}
ALLOWED_IMPORT_WAVES = {
    "wave_1_activity",
    "wave_2_toxicology",
    "wave_3_multiomics",
}


class PublicRegistryError(ValueError):
    pass


def load_registry(path: Path = DEFAULT_PATH) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PublicRegistryError("registry root must be an object")
    if payload.get("schema_version") != "molmind-public-data-registry-v1":
        raise PublicRegistryError("unsupported public-data registry schema")
    policy = payload.get("project_policy")
    if not isinstance(policy, dict):
        raise PublicRegistryError("project_policy must be an object")
    if policy.get("missing_record_semantics") != "audit_missing":
        raise PublicRegistryError("missing records must remain audit_missing")
    if policy.get("negative_search_is_negative_label") is not False:
        raise PublicRegistryError("negative search must not become a negative label")
    required = set(policy.get("required_record_fields") or [])
    missing = {"compound_id", "standardized_smiles", "source_id", "assay_id"} - required
    if missing:
        raise PublicRegistryError(f"required record fields missing: {sorted(missing)}")
    wave_order = list(policy.get("import_wave_order") or [])
    if wave_order != [
        "wave_1_activity",
        "wave_2_toxicology",
        "wave_3_multiomics",
    ]:
        raise PublicRegistryError(
            "import_wave_order must be "
            "[wave_1_activity, wave_2_toxicology, wave_3_multiomics]"
        )
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise PublicRegistryError("sources must be a non-empty list")
    seen: set[str] = set()
    previous_wave_index = -1
    for source in sources:
        if not isinstance(source, dict):
            raise PublicRegistryError("each source must be an object")
        source_id = str(source.get("source_id") or "")
        if not source_id or source_id in seen:
            raise PublicRegistryError(f"duplicate or empty source_id: {source_id!r}")
        seen.add(source_id)
        if source.get("role") not in ALLOWED_ROLES:
            raise PublicRegistryError(f"{source_id}: unsupported role")
        import_wave = str(source.get("import_wave") or "")
        if import_wave not in ALLOWED_IMPORT_WAVES:
            raise PublicRegistryError(f"{source_id}: unsupported or missing import_wave")
        wave_index = wave_order.index(import_wave)
        if wave_index < previous_wave_index:
            raise PublicRegistryError(
                f"{source_id}: sources must appear in import_wave_order "
                "(activity → toxicology → multiomics)"
            )
        previous_wave_index = wave_index
        for field in ("display_name", "source_url", "license_policy", "ingestion_status", "ranking_effect"):
            if not str(source.get(field) or "").strip():
                raise PublicRegistryError(f"{source_id}: missing {field}")
        if source.get("missing_semantics") != "audit_missing":
            raise PublicRegistryError(f"{source_id}: missing semantics must be audit_missing")
        if source["role"] in {"mechanism_support", "mechanism_support_or_assay_qc"} and source["ranking_effect"] != "mechanism_support_only":
            raise PublicRegistryError(f"{source_id}: mechanism source cannot directly affect ranking")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, default=DEFAULT_PATH)
    args = parser.parse_args()
    payload = load_registry(args.registry)
    print(json.dumps({
        "schema_version": payload["schema_version"],
        "source_count": len(payload["sources"]),
        "source_ids": [source["source_id"] for source in payload["sources"]],
        "import_wave_order": payload["project_policy"]["import_wave_order"],
        "quality_first": payload["project_policy"]["quality_first"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
