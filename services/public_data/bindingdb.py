"""BindingDB Wave-1 importer: lipid-mechanism UniProt binding rows.

Binding affinity is ``mechanism_support`` only. Presence of a Ki/IC50/Kd never
implies cellular lipid-lowering or safety clearance. Network failures stay
``audit_missing`` / ``network_error`` and are never written as negatives.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from rdkit import Chem

ASSAY_GRAIN_FIELDS = (
    "compound_id",
    "standardized_smiles",
    "source_id",
    "assay_id",
    "endpoint",
    "dose",
    "dose_unit",
    "treatment_time_hours",
    "direction",
    "value",
    "unit",
    "control_id",
    "batch_id",
    "source_url",
    "retrieved_at",
    "license",
)

# Human lipid / MASLD-relevant targets (UniProt accession → short label).
DEFAULT_LIPID_UNIPROTS: Tuple[Tuple[str, str], ...] = (
    ("Q07869", "PPARA"),
    ("P37231", "PPARG"),
    ("Q03181", "PPARD"),
    ("P04035", "HMGCR"),
    ("P49327", "FASN"),
    ("O00767", "SCD1"),
    ("O75907", "DGAT1"),
    ("Q96PD7", "DGAT2"),
    ("Q13085", "ACACA"),
    ("O00763", "ACACB"),
    ("P50416", "CPT1A"),
    ("Q9UHC9", "NPC1L1"),
    ("P01130", "LDLR"),
    ("P06858", "LPL"),
    ("O95477", "ABCA1"),
    ("Q13131", "PRKAA1"),
)

DEFAULT_API_BASE = "https://www.bindingdb.org/rest"
DEFAULT_AFFINITY_CUTOFF_NM = 1000
JsonGetter = Callable[[str, Optional[Dict[str, Any]], int], Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _curl_get_json(full_url: str, timeout: int) -> Any:
    """Prefer curl for BindingDB: urllib often stalls on long JSON responses."""
    completed = subprocess.run(
        [
            "curl",
            "-sS",
            "-L",
            "--max-time",
            str(max(30, int(timeout))),
            "-A",
            "MolMind-public-import/1.1",
            "-H",
            "Accept: application/json",
            full_url,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise URLError(completed.stderr.strip() or f"curl exit {completed.returncode}")
    raw = (completed.stdout or "").strip()
    if not raw:
        return {}
    return json.loads(raw)


def default_get_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 60,
    *,
    retries: int = 2,
    backoff_sec: float = 1.5,
) -> Any:
    query = dict(params or {})
    full = f"{url}?{urlencode(query, doseq=True)}" if query else url
    last_error: Optional[Exception] = None
    for attempt in range(max(1, retries)):
        try:
            return _curl_get_json(full, timeout)
        except (URLError, TimeoutError, json.JSONDecodeError, FileNotFoundError) as exc:
            last_error = exc
            # Fallback to urllib once if curl is unavailable.
            if isinstance(exc, FileNotFoundError) or "curl" in str(exc).lower():
                try:
                    request = Request(
                        full,
                        headers={
                            "User-Agent": "MolMind-public-import/1.1",
                            "Accept": "application/json",
                        },
                    )
                    with urlopen(request, timeout=timeout) as response:  # noqa: S310
                        raw = response.read().decode("utf-8", errors="replace").strip()
                    return json.loads(raw) if raw else {}
                except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as urllib_exc:
                    last_error = urllib_exc
            if attempt + 1 >= retries:
                break
            time.sleep(backoff_sec * (attempt + 1))
    assert last_error is not None
    raise last_error


def parse_affinity_nM(raw: Any) -> Tuple[Optional[float], Optional[str]]:
    """Parse BindingDB affinity text; keep inequality in qualifier."""
    if raw is None:
        return None, None
    text = str(raw).strip().replace(",", "")
    if not text:
        return None, None
    match = re.match(r"^(<|>|<=|>=|~)?\s*([0-9]*\.?[0-9]+)\s*$", text)
    if not match:
        return None, None
    qualifier = match.group(1) or None
    try:
        return float(match.group(2)), qualifier
    except ValueError:
        return None, None


def smiles_to_inchikey(smiles: str) -> Optional[str]:
    mol = Chem.MolFromSmiles(smiles or "")
    if mol is None:
        return None
    return Chem.MolToInchiKey(mol) or None


def _extract_affinities(payload: Any) -> List[dict[str, Any]]:
    if not payload:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    # BindingDB currently uses the typo'd key getLindsByUniprotsResponse.
    for key in (
        "getLindsByUniprotsResponse",
        "getLigandsByUniprotsResponse",
        "getLigandsByUniprotResponse",
    ):
        block = payload.get(key)
        if isinstance(block, dict):
            affinities = block.get("affinities")
            if isinstance(affinities, list):
                return [row for row in affinities if isinstance(row, dict)]
            if isinstance(affinities, dict):
                return [affinities]
    affinities = payload.get("affinities")
    if isinstance(affinities, list):
        return [row for row in affinities if isinstance(row, dict)]
    return []


def normalize_bindingdb_row(
    row: dict[str, Any],
    *,
    source_id: str,
    license_policy: str,
    uniprot: str,
    target_label: str,
    api_base: str,
    retrieved_at: Optional[str] = None,
) -> Dict[str, Any]:
    smiles = str(row.get("smile") or row.get("smiles") or "").strip()
    monomer = str(row.get("monomerid") or row.get("monomerId") or "").strip()
    endpoint = str(row.get("affinity_type") or row.get("affinityType") or "affinity").strip()
    value, qualifier = parse_affinity_nM(row.get("affinity"))
    inchikey = smiles_to_inchikey(smiles) if smiles else None
    assay_id = f"BindingDB:{uniprot}:{endpoint}"
    record = {
        "compound_id": f"BindingDB:{monomer}" if monomer else None,
        "standardized_smiles": smiles or None,
        "inchikey": inchikey,
        "source_id": source_id,
        "assay_id": assay_id,
        "endpoint": endpoint,
        "dose": None,
        "dose_unit": None,
        "treatment_time_hours": None,
        "direction": "unknown",
        "value": value,
        "unit": "nM" if value is not None else None,
        "control_id": None,
        "batch_id": row.get("pmid") or row.get("doi"),
        "source_url": (
            f"{api_base.rstrip('/')}/getLigandsByUniprots?"
            f"uniprot={uniprot}&cutoff=&response=application/json"
        ),
        "retrieved_at": retrieved_at or _utc_now(),
        "license": license_policy,
        "monomer_id": monomer or None,
        "uniprot": uniprot,
        "target_label": target_label,
        "target_name": row.get("query") or target_label,
        "affinity_raw": row.get("affinity"),
        "affinity_qualifier": qualifier,
        "pmid": row.get("pmid"),
        "doi": row.get("doi"),
        "classification": "mechanism",
        "evidence_role": "mechanism_support",
        "molmind_direction": "unknown",
    }
    return record


def _record_key(row: dict[str, Any]) -> str:
    return "|".join(
        [
            str(row.get("compound_id") or ""),
            str(row.get("uniprot") or ""),
            str(row.get("endpoint") or ""),
            str(row.get("affinity_raw") or row.get("value") or ""),
            str(row.get("pmid") or ""),
        ]
    )


def import_bindingdb_assay_grain(
    source: dict[str, Any],
    *,
    limit: int = 40,
    per_target_limit: int = 25,
    affinity_cutoff_nM: int = DEFAULT_AFFINITY_CUTOFF_NM,
    uniprots: Optional[Iterable[Tuple[str, str]]] = None,
    timeout: int = 60,
    get_json: Optional[JsonGetter] = None,
    cache_dir: Optional[Path] = None,
    raw_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Fetch BindingDB ligands for curated lipid UniProt targets.

    ``limit`` caps total retained assay-grain rows after dedupe. Empty targets
    are audited; they are not negative labels.
    """
    getter = get_json or default_get_json
    api_base = str(source.get("api_base") or DEFAULT_API_BASE)
    targets = list(uniprots or DEFAULT_LIPID_UNIPROTS)
    retrieved_at = _utc_now()
    records_by_key: dict[str, dict[str, Any]] = {}
    target_stats: list[dict[str, Any]] = []
    target_errors: list[dict[str, Any]] = []
    raw_pages: list[dict[str, Any]] = []

    # Collect per-target candidate rows first, then round-robin so PPAR does not
    # consume the entire ``limit`` budget before HMGCR/FASN/DGAT appear.
    per_target_rows: list[tuple[str, str, list[dict[str, Any]], int, bool]] = []
    for uniprot, label in targets:
        cache_path = Path(cache_dir) / f"{uniprot}.json" if cache_dir else None
        payload: Any = None
        try:
            if cache_path is not None and cache_path.is_file():
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
            else:
                payload = getter(
                    f"{api_base.rstrip('/')}/getLigandsByUniprots",
                    {
                        "uniprot": uniprot,
                        "cutoff": int(affinity_cutoff_nM),
                        "response": "application/json",
                    },
                    timeout,
                )
                if cache_dir is not None:
                    Path(cache_dir).mkdir(parents=True, exist_ok=True)
                    (Path(cache_dir) / f"{uniprot}.json").write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
        except Exception as exc:
            target_errors.append(
                {
                    "uniprot": uniprot,
                    "target_label": label,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            continue

        affinities = _extract_affinities(payload)
        raw_pages.append(
            {
                "uniprot": uniprot,
                "target_label": label,
                "affinity_count": len(affinities),
                "payload_preview_keys": list(payload)[:8] if isinstance(payload, dict) else [],
            }
        )
        candidates: list[dict[str, Any]] = []
        for row in affinities:
            if len(candidates) >= per_target_limit:
                break
            record = normalize_bindingdb_row(
                row,
                source_id=source["source_id"],
                license_policy=str(source.get("license_policy") or ""),
                uniprot=uniprot,
                target_label=label,
                api_base=api_base,
                retrieved_at=retrieved_at,
            )
            if not record.get("compound_id") or not record.get("standardized_smiles"):
                continue
            candidates.append(record)
        per_target_rows.append(
            (
                uniprot,
                label,
                candidates,
                len(affinities),
                bool(cache_path and cache_path.is_file()),
            )
        )

    # Round-robin across targets for diversity within the global limit.
    indexes = {label: 0 for _, label, _, _, _ in per_target_rows}
    progressed = True
    while len(records_by_key) < limit and progressed:
        progressed = False
        for uniprot, label, candidates, returned, cache_hit in per_target_rows:
            idx = indexes[label]
            if idx >= len(candidates):
                continue
            record = candidates[idx]
            indexes[label] = idx + 1
            records_by_key[_record_key(record)] = record
            progressed = True
            if len(records_by_key) >= limit:
                break

    for uniprot, label, candidates, returned, cache_hit in per_target_rows:
        target_stats.append(
            {
                "uniprot": uniprot,
                "target_label": label,
                "returned": returned,
                "kept": indexes.get(label, 0),
                "cache_hit": cache_hit,
            }
        )

    records = list(records_by_key.values())
    for record in records:
        missing = [field for field in ASSAY_GRAIN_FIELDS if field not in record]
        if missing:
            raise ValueError(f"assay-grain record missing fields: {missing}")

    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time())
        raw_path = raw_dir / f"bindingdb_assay_grain_{stamp}.json"
        raw_path.write_text(
            json.dumps(
                {
                    "api_base": api_base,
                    "affinity_cutoff_nM": affinity_cutoff_nM,
                    "targets": [{"uniprot": u, "label": lab} for u, lab in targets],
                    "target_stats": target_stats,
                    "target_errors": target_errors,
                    "pages": raw_pages,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    else:
        raw_path = None

    if not records:
        raise RuntimeError(
            "bindingdb import produced zero records "
            f"(target_errors={len(target_errors)}); "
            "refusing to treat network failure as an empty negative table"
        )

    label_counts: Dict[str, int] = {}
    for row in records:
        key = str(row.get("target_label") or "unknown")
        label_counts[key] = label_counts.get(key, 0) + 1

    return {
        "records": records,
        "activity_count": len(records),
        "target_count": len(target_stats),
        "target_stats": target_stats,
        "target_errors": target_errors,
        "target_label_counts": label_counts,
        "affinity_cutoff_nM": affinity_cutoff_nM,
        "api_base": api_base,
        "raw_path": str(raw_path) if raw_path else None,
        "grain": "compound_x_assay_x_activity",
        "ranking_effect": "mechanism_support_only",
        "note": (
            "BindingDB lipid-mechanism UniProt subset. Affinity is mechanism "
            "support only; binding ≠ cellular lipid-lowering. Empty/failed "
            "targets are audit_missing, not negatives."
        ),
    }
