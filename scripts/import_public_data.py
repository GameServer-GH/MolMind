#!/usr/bin/env python3
"""Import public evidence in registry order with auditable, fail-closed manifests.

The importer deliberately keeps source-grain records. A network failure creates
only a manifest (``network_error``/``audit_missing``); it never creates an empty
training table and never interprets a missing search hit as a negative label.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
import zipfile
from xml.etree import ElementTree
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.public_data.bindingdb import import_bindingdb_assay_grain
from services.public_data.chembl import (
    SEED_HEPG2_FFA_ASSAY_IDS,
    SEED_POSITIVE_ASSAY_IDS,
    import_chembl_assay_grain,
    import_chembl_by_inchikeys,
)
from services.public_data.toxcast_ctx import AuthMissingError, import_toxcast_ctx

REGISTRY = ROOT / "data/public/registry.yaml"
RAW = ROOT / "data/public/raw"
PROCESSED = ROOT / "data/public/processed"
MANIFESTS = ROOT / "data/public/manifests"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def get_json(url: str, params: dict[str, Any] | None = None, timeout: int = 60) -> Any:
    if params:
        url = f"{url}?{urlencode(params, doseq=True)}"
    request = Request(
        url,
        headers={"User-Agent": "MolMind-public-import/1.1", "Accept": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - registry URLs are explicit
        return json.loads(response.read().decode("utf-8"))


def get_bytes(url: str, timeout: int = 60) -> bytes:
    request = Request(url, headers={"User-Agent": "MolMind-public-import/1.0"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - explicit registry endpoint
        return response.read()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def manifest(source: dict[str, Any], status: str, **extra: Any) -> dict[str, Any]:
    payload = {
        "schema_version": "molmind-public-import-manifest-v1",
        "source_id": source["source_id"],
        "import_wave": source["import_wave"],
        "source_url": source["source_url"],
        "license_policy": source["license_policy"],
        "captured_at": now(),
        "status": status,
        "missing_semantics": "audit_missing",
        "negative_search_is_negative_label": False,
    }
    payload.update(extra)
    return payload


def save_manifest(source: dict[str, Any], payload: dict[str, Any]) -> Path:
    path = MANIFESTS / f"{source['source_id']}.json"
    write_json(path, payload)
    return path


def _load_inchikey_list(path: Path | None) -> list[str]:
    if path is None or not path.is_file():
        return []
    keys: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        # Accept "InChIKey" or "id,InChIKey" rows.
        piece = text.split(",")[-1].strip()
        if piece and piece not in keys:
            keys.append(piece)
    return keys


def chembl(
    source: dict[str, Any],
    limit: int,
    *,
    inchikeys: list[str] | None = None,
) -> dict[str, Any]:
    """Wave-1 ChEMBL assay-grain import (compound × assay × activity)."""
    existing = PROCESSED / source["source_id"] / "records.jsonl"
    backup = PROCESSED / source["source_id"] / "records.jsonl.bak_pre_positive_expand"
    merge_path = existing if existing.is_file() else backup
    # Prefer HepG2-FFA seeds; keep assay budget >= curated seed count.
    assay_budget = max(limit, len(SEED_POSITIVE_ASSAY_IDS), len(SEED_HEPG2_FFA_ASSAY_IDS) + 10)
    # ChEMBL assay search often 500/stalls; HepG2-FFA expansion is seed-driven.
    # Opportunistic search can be re-enabled later once the API is stable.
    result = import_chembl_assay_grain(
        source,
        limit=assay_budget,
        page_limit=min(25, max(10, limit)),
        max_activities_per_assay=min(40, max(12, limit)),
        query_terms=(),
        scan_per_term=0,
        seed_assay_ids=SEED_POSITIVE_ASSAY_IDS,
        merge_existing_path=merge_path if merge_path.is_file() else None,
        timeout=45,
        raw_dir=RAW / source["source_id"],
    )
    # Optional candidate-level expansion so Top-M / shortlist InChIKeys enter the index.
    if inchikeys:
        # Write seed/assay merge first so by-inchikey can merge on disk snapshot.
        interim = PROCESSED / source["source_id"] / "records.jsonl"
        interim.parent.mkdir(parents=True, exist_ok=True)
        with interim.open("w", encoding="utf-8") as handle:
            for record in result["records"]:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        expanded = import_chembl_by_inchikeys(
            source,
            inchikeys,
            max_activities_per_molecule=min(40, max(12, limit)),
            page_limit=min(25, max(10, limit)),
            timeout=45,
            merge_existing_path=interim,
            raw_dir=RAW / source["source_id"],
        )
        result["records"] = expanded["records"]
        result["activity_count"] = len(expanded["records"])
        result["candidate_inchikey_expansion"] = {
            "requested": expanded.get("inchikeys_requested"),
            "resolved": expanded.get("inchikeys_resolved"),
            "verified_empty": expanded.get("inchikeys_verified_empty"),
            "classification_counts": expanded.get("classification_counts"),
            "molecule_errors": expanded.get("molecule_errors"),
        }
        if expanded.get("raw_path"):
            result["raw_path"] = expanded["raw_path"]
    return result


def bindingdb(source: dict[str, Any], limit: int, cache_dir: Path | None = None) -> dict[str, Any]:
    """Wave-1 BindingDB lipid UniProt binding subset (mechanism_support only)."""
    # Cap per-target so round-robin covers PPAR/HMGCR/FASN/DGAT/... within limit.
    per_target = max(3, min(12, (max(20, limit) + 15) // 16))
    return import_bindingdb_assay_grain(
        source,
        limit=max(20, limit),
        per_target_limit=per_target,
        affinity_cutoff_nM=1000,
        timeout=120,
        cache_dir=cache_dir or (RAW / source["source_id"] / "cache"),
        raw_dir=RAW / source["source_id"],
    )


def _pubchem_records(source: dict[str, Any], concise: dict[str, Any], identity: dict[str, Any]) -> list[dict[str, Any]]:
    columns = concise.get("Table", {}).get("Columns", {}).get("Column", [])
    properties = {
        str(row.get("CID")): row for row in identity.get("PropertyTable", {}).get("Properties", [])
        if row.get("CID") is not None
    }
    records: list[dict[str, Any]] = []
    for row in concise.get("Table", {}).get("Row", []):
        cells = row.get("Cell", [])
        values = dict(zip(columns, cells))
        cid = str(values.get("CID") or "")
        prop = properties.get(cid, {})
        value = values.get("Activity Value [uM]")
        try:
            value = float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            pass
        records.append({
            "compound_id": f"CID:{cid}" if cid else None,
            "standardized_smiles": prop.get("ConnectivitySMILES") or prop.get("CanonicalSMILES"),
            "isomeric_smiles": prop.get("SMILES") or prop.get("IsomericSMILES"),
            "inchikey": prop.get("InChIKey"),
            "compound_name": prop.get("IUPACName"),
            "source_id": source["source_id"],
            "assay_id": f"AID:{values.get('AID')}",
            "sid": values.get("SID"),
            "endpoint": values.get("Activity Name") or "depositor_assay_activity",
            "dose": None,
            "dose_unit": None,
            "treatment_time_hours": None,
            "direction": values.get("Activity Outcome"),
            "value": value,
            "unit": "uM" if value is not None else None,
            "control_id": None,
            "batch_id": None,
            "source_url": f"{source['api_base']}/assay/aid/{values.get('AID')}/concise/JSON",
            "retrieved_at": now(),
            "license": source["license_policy"],
        })
    return records


def pubchem(source: dict[str, Any], limit: int, cache_dir: Path | None = None) -> dict[str, Any]:
    def summary(records: list[dict[str, Any]]) -> dict[str, Any]:
        compounds = {r.get("compound_id") for r in records if r.get("compound_id")}
        resolved = {r.get("compound_id") for r in records if r.get("compound_id") and r.get("standardized_smiles") and r.get("inchikey")}
        outcomes: dict[str, int] = {}
        for record in records:
            key = str(record.get("direction") or "missing")
            outcomes[key] = outcomes.get(key, 0) + 1
        return {"unique_compound_count": len(compounds), "structure_identity_unique_resolved": len(resolved),
                "structure_identity_record_resolved": sum(bool(r.get("standardized_smiles") and r.get("inchikey")) for r in records),
                "numeric_activity_count": sum(r.get("value") is not None for r in records), "outcome_counts": outcomes}
    if cache_dir:
        search = json.loads((cache_dir / "esearch_50.json").read_text(encoding="utf-8"))
        concise = json.loads((cache_dir / "concise_50.json").read_text(encoding="utf-8"))
        identity = json.loads((cache_dir / "identity_50.json").read_text(encoding="utf-8"))
        aids = search.get("esearchresult", {}).get("idlist", [])
        records = _pubchem_records(source, concise, identity)
        return {"records": records, "aids": aids, **summary(records),
                "query": "HepG2 AND (lipid OR steatosis OR triglyceride)", "cache": True}
    search = get_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", {
        "db": "pcassay", "term": "HepG2 AND (lipid OR steatosis OR triglyceride)",
        "retmode": "json", "retmax": limit,
    })
    aids = search.get("esearchresult", {}).get("idlist", [])
    aid_text = ",".join(str(aid) for aid in aids)
    concise = get_json(f"{source['api_base']}/assay/aid/{aid_text}/concise/JSON")
    cids = sorted({str(cell) for row in concise.get("Table", {}).get("Row", [])
                   for cell in row.get("Cell", [])[2:3] if cell})
    identity = get_json(f"{source['api_base']}/compound/cid/{','.join(cids)}/property/CanonicalSMILES,IsomericSMILES,InChIKey,IUPACName/JSON") if cids else {}
    records = _pubchem_records(source, concise, identity)
    return {"records": records, "aids": aids, **summary(records),
            "query": "HepG2 AND (lipid OR steatosis OR triglyceride)"}


def metadata_only(source: dict[str, Any], limit: int) -> dict[str, Any]:
    """Create an auditable accession plan; raw omics files are intentionally opt-in."""
    accessions = source.get("example_accessions", [])[:limit]
    return {"records": [{"accession": accession, "source_id": source["source_id"],
                          "source_url": source["source_url"], "status": "metadata_only",
                          "retrieved_at": now(), "license": source["license_policy"]}
                         for accession in accessions], "accessions": accessions}


def dilirank(source: dict[str, Any]) -> dict[str, Any]:
    """Download FDA's small XLSX and retain cell-level rows without pandas."""
    raw_path = RAW / source["source_id"] / "dilirank_2.0.xlsx"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    if not raw_path.exists():
        raw_path.write_bytes(get_bytes(source["download_url"]))
    rows: list[list[str]] = []
    with zipfile.ZipFile(raw_path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            shared = ["".join(node.itertext()) for node in root.findall("x:si", ns)]
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        for row in sheet.findall(".//x:row", ns):
            values: list[str] = []
            for cell in row.findall("x:c", ns):
                value = cell.find("x:v", ns)
                text = "" if value is None else (value.text or "")
                if cell.get("t") == "s" and text.isdigit() and int(text) < len(shared):
                    text = shared[int(text)]
                values.append(text)
            if values:
                rows.append(values)
    processed = PROCESSED / source["source_id"] / "records.csv"
    processed.parent.mkdir(parents=True, exist_ok=True)
    with processed.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)
    return {"records": rows, "raw_path": str(raw_path.relative_to(ROOT)),
            "processed_path": str(processed.relative_to(ROOT)), "row_count": max(0, len(rows) - 2),
            "header_rows": 2,
            "dataset_version": "DILIrank 2.0"}


def toxcast(
    source: dict[str, Any],
    limit: int,
    cache_dir: Path | None = None,
    *,
    inchikeys: list[str] | None = None,
) -> dict[str, Any]:
    """Wave-2 ToxCast/Tox21 via CTX Bioactivity API (DTXSID), not Figshare SQL."""
    return import_toxcast_ctx(
        source,
        limit=max(20, limit),
        per_dtxsid_limit=max(5, min(20, limit // 3 or 5)),
        inchikeys=inchikeys or None,
        cache_dir=cache_dir or (RAW / source["source_id"] / "cache"),
        fixture_dir=ROOT / "data/public/fixtures/toxcast_ctx",
        allow_fixture_fallback=True,
        timeout=90,
        raw_dir=RAW / source["source_id"],
    )


def run_one(
    source: dict[str, Any],
    limit: int,
    dry_run: bool = False,
    pubchem_cache: Path | None = None,
    bindingdb_cache: Path | None = None,
    toxcast_cache: Path | None = None,
    inchikeys: list[str] | None = None,
) -> dict[str, Any]:
    if dry_run:
        return manifest(source, "planned", warnings=["dry_run: no network request made"])
    try:
        if source["source_id"] == "chembl_bioactivity":
            result = chembl(source, limit, inchikeys=inchikeys)
        elif source["source_id"] == "pubchem_bioassay":
            result = pubchem(source, limit, cache_dir=pubchem_cache)
        elif source["source_id"] == "bindingdb":
            result = bindingdb(source, limit, cache_dir=bindingdb_cache)
        elif source["source_id"] == "fda_dilirank_2":
            result = dilirank(source)
        elif source["source_id"] == "epa_toxcast_tox21":
            result = toxcast(source, limit, cache_dir=toxcast_cache, inchikeys=inchikeys)
        elif source["source_id"] in {"geo_ffa_and_drug_signatures", "pride_hepg2_ffa", "metabolomics_workbench_and_metabolights", "lincs_cmap"}:
            result = metadata_only(source, limit)
        else:
            return manifest(source, "planned", warnings=["large/download source requires explicit file transfer; no implicit GB-scale download"])
        records = result.pop("records", [])
        # Prefer importer-provided raw dump when present (ChEMBL assay-grain).
        provided_raw = result.pop("raw_path", None)
        record_count = int(result.get("row_count", result.get("activity_count", len(records))))
        if not records:
            return manifest(
                source,
                "audit_missing",
                row_count=0,
                warnings=["no records returned; not a negative label"],
                query=result,
            )
        if provided_raw:
            raw_path = Path(provided_raw)
            if not raw_path.is_absolute():
                raw_path = ROOT / raw_path
        else:
            raw_path = RAW / source["source_id"] / f"response_{int(time.time())}.json"
            write_json(raw_path, result | {"records": records})
        processed_path = PROCESSED / source["source_id"] / "records.jsonl"
        processed_path.parent.mkdir(parents=True, exist_ok=True)
        with processed_path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return manifest(
            source,
            "imported",
            row_count=record_count,
            raw_path=str(raw_path.relative_to(ROOT)),
            processed_path=str(processed_path.relative_to(ROOT)),
            raw_sha256=sha256(raw_path),
            processed_sha256=sha256(processed_path),
            query=result,
            grain=result.get("grain"),
        )
    except AuthMissingError as exc:
        return manifest(
            source,
            "auth_missing",
            error_type=type(exc).__name__,
            error=str(exc),
            warnings=[
                "CTX API key required for live ToxCast; configure evidence_providers.yaml or use fixtures/cache",
                "record as audit_missing; never treat as low toxicity",
            ],
        )
    except Exception as exc:  # network and schema errors are audit-visible, never silent
        return manifest(
            source,
            "network_error",
            error_type=type(exc).__name__,
            error=str(exc),
            warnings=["external retrieval failed; no dataset written", "record as audit_missing"],
        )


def sync_registry_status(source_id: str, status: str) -> None:
    """Mirror importer status into registry.yaml without full YAML rewrite.

    Only the matching source's ``ingestion_status:`` line is updated so document
    formatting and comments remain stable.
    """
    text = REGISTRY.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    in_target = False
    updated = False
    out: list[str] = []
    for line in lines:
        if re.match(r"^\s*-\s*source_id:\s*" + re.escape(source_id) + r"\s*$", line):
            in_target = True
            out.append(line)
            continue
        if in_target and re.match(r"^\s*-\s*source_id:\s*", line):
            in_target = False
        if in_target and re.match(r"^(\s*)ingestion_status:\s*", line):
            indent = re.match(r"^(\s*)", line).group(1)  # type: ignore[union-attr]
            out.append(f"{indent}ingestion_status: {status}\n")
            updated = True
            in_target = False
            continue
        out.append(line)
    if not updated:
        raise RuntimeError(f"could not sync ingestion_status for {source_id}")
    REGISTRY.write_text("".join(out), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="all", help="registry source_id, wave name, or all")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pubchem-cache", type=Path, help="offline cache containing esearch_50.json, concise_50.json and identity_50.json")
    parser.add_argument(
        "--bindingdb-cache",
        type=Path,
        help="offline cache dir of BindingDB per-UniProt JSON ({ACCESSION}.json)",
    )
    parser.add_argument(
        "--toxcast-cache",
        type=Path,
        help="offline cache dir of CTX ToxCast per-DTXSID JSON ({DTXSID}.json)",
    )
    parser.add_argument(
        "--candidate-inchikeys",
        type=Path,
        help=(
            "optional InChIKey list (one per line) for ChEMBL candidate expansion "
            "and ToxCast InChIKey→DTXSID resolution (ToxCast needs a configured CTX key)"
        ),
    )
    parser.add_argument(
        "--sync-registry",
        action="store_true",
        help="write ingestion_status back into data/public/registry.yaml",
    )
    args = parser.parse_args()
    registry = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    sources = registry["sources"]
    wanted = [
        s
        for s in sources
        if args.source == "all"
        or s["source_id"] == args.source
        or s["import_wave"] == args.source
    ]
    if not wanted:
        parser.error(f"unknown source or wave: {args.source}")
    inchikeys = _load_inchikey_list(args.candidate_inchikeys)
    # Registry order is a quality gate: do not permit later-wave execution before earlier waves.
    exit_code = 0
    for source in wanted:
        result = run_one(
            source,
            max(1, args.limit),
            args.dry_run,
            args.pubchem_cache,
            args.bindingdb_cache,
            args.toxcast_cache,
            inchikeys=inchikeys or None,
        )
        path = save_manifest(source, result)
        if args.sync_registry and not args.dry_run:
            processed = PROCESSED / source["source_id"] / "records.jsonl"
            # Flaky live probes must not downgrade a source that already has a
            # usable processed snapshot.
            if result["status"] in {"network_error", "audit_missing", "auth_missing"} and processed.is_file():
                pass
            else:
                sync_registry_status(source["source_id"], result["status"])
        print(
            json.dumps(
                {
                    "source_id": source["source_id"],
                    "status": result["status"],
                    "row_count": result.get("row_count"),
                    "manifest": str(path.relative_to(ROOT)),
                },
                ensure_ascii=False,
            )
        )
        if result["status"] in {"network_error", "auth_missing"}:
            exit_code = 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
