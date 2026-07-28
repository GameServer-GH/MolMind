"""ChEMBL Wave-1 assay-grain importer.

Produces one row per ``compound × assay × activity`` with the registry required
fields filled when available. Missing optional fields stay null / empty and are
never fabricated into negative or low-toxicity labels.
"""

from __future__ import annotations

from plugins.molmind_core.scientific.paths import REPO_ROOT
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from plugins.molmind_core.scientific.evidence_gateway import EvidenceQueryCache
from plugins.molmind_core.scientific.evidence_facade.facade import (
    CELL_CONTEXT_RE,
    LIPID_ENDPOINT_RE,
    POSITIVE_LIPID_DIRECTION_RE,
    _classify_chembl_activity,
)

ROOT = REPO_ROOT

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

# Prefer cellular lipid-lowering recall. Multi-word AND phrases often 500/empty on
# ChEMBL; keep short terms and prioritize client-side.
DEFAULT_QUERY_TERMS = (
    "oleic",
    "palmitic",
    "triglyceride accumulation",
    "lipid accumulation",
    "lipid droplet",
    "neutral lipid",
    "Oil Red",
    "steatosis",
)

# HepG2 + FFA/oleate/palmitate induced lipid/TG reduction (MASLD proxy phenotype).
SEED_HEPG2_FFA_ASSAY_IDS = (
    "CHEMBL2156870",
    "CHEMBL3107293",
    "CHEMBL3107294",
    "CHEMBL3372774",
    "CHEMBL3372894",
    "CHEMBL3372895",
    "CHEMBL3372896",
    "CHEMBL3373471",
    "CHEMBL3373472",
    "CHEMBL3373473",
    "CHEMBL3373488",
    "CHEMBL3373489",
    "CHEMBL5322681",
    "CHEMBL5322682",
    "CHEMBL5322683",
    "CHEMBL5322684",
    "CHEMBL5322685",
    "CHEMBL5389126",
    "CHEMBL5546883",
)

# HepG2 cellular lipid/TG reduction without explicit FFA induction phrasing.
SEED_HEPG2_LIPID_ASSAY_IDS = (
    "CHEMBL2328361",
    "CHEMBL2328362",
    "CHEMBL2328363",
    "CHEMBL2341480",
    "CHEMBL2341481",
    "CHEMBL1107086",
    "CHEMBL3606720",
    "CHEMBL4735031",
    "CHEMBL4735033",
    "CHEMBL5546885",
)

# Small 3T3-L1 antiadipogenic set kept for diversity (not HepG2-FFA proxy).
SEED_ADIPO_ASSAY_IDS = (
    "CHEMBL1115929",
    "CHEMBL1225937",
    "CHEMBL2346149",
)

# Curated positive phenotype seeds; HepG2-FFA first when ChEMBL search is flaky.
SEED_POSITIVE_ASSAY_IDS = (
    *SEED_HEPG2_FFA_ASSAY_IDS,
    *SEED_HEPG2_LIPID_ASSAY_IDS,
    *SEED_ADIPO_ASSAY_IDS,
)

_FFA_MODEL_RE = re.compile(
    r"oleic|palmit|FFA|free fatty|fatty acid|sodium oleate|sodium palmitate",
    re.I,
)

DIRECTION_BY_CLASS = {
    "positive_phenotype": "supports",
    "adverse_phenotype": "risk",
    "mechanism": "unknown",
    "annotation": "unknown",
}

_CLASS_PRIORITY = {
    "positive_phenotype": 0,
    "adverse_phenotype": 2,
    "mechanism": 3,
    "annotation": 4,
}

JsonGetter = Callable[[str, Optional[Dict[str, Any]], int], Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_get_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 60,
    *,
    retries: int = 3,
    backoff_sec: float = 1.5,
) -> Any:
    """HTTP GET JSON with retries. ChEMBL requires ``format=json``."""
    query = dict(params or {})
    query.setdefault("format", "json")
    full = f"{url}?{urlencode(query, doseq=True)}"
    last_error: Optional[Exception] = None
    for attempt in range(max(1, retries)):
        try:
            request = Request(
                full,
                headers={
                    "User-Agent": "MolMind-public-import/1.1",
                    "Accept": "application/json",
                },
            )
            with urlopen(request, timeout=timeout) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 >= retries:
                break
            time.sleep(backoff_sec * (attempt + 1))
    assert last_error is not None
    raise last_error


def _parse_treatment_hours(text: str) -> Optional[float]:
    import re

    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b", text or "", re.I)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def normalize_chembl_activity_row(
    activity: dict[str, Any],
    *,
    source_id: str,
    license_policy: str,
    api_base: str,
    retrieved_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Map a ChEMBL activity document onto the registry assay-grain contract."""
    classification = _classify_chembl_activity(activity)
    assay_text = " ".join(
        str(activity.get(key) or "")
        for key in ("assay_description", "assay_type", "bao_label", "activity_comment")
    )
    assay_id = str(activity.get("assay_chembl_id") or "").strip()
    compound_id = str(activity.get("molecule_chembl_id") or "").strip() or None
    smiles = activity.get("canonical_smiles")
    smiles_text = str(smiles).strip() if smiles else ""
    value = activity.get("standard_value")
    unit = activity.get("standard_units")
    # For concentration-response types, measured value is the response; dose may
    # be absent in ChEMBL. Keep both fields explicit rather than inventing dose.
    dose = activity.get("standard_value") if str(activity.get("standard_type") or "").upper() in {
        "CONC",
        "DOSE",
    } else None
    dose_unit = unit if dose is not None else None
    record = {
        "compound_id": compound_id,
        "standardized_smiles": smiles_text or None,
        "source_id": source_id,
        "assay_id": assay_id or None,
        "endpoint": activity.get("standard_type") or activity.get("bao_label") or "activity",
        "dose": dose,
        "dose_unit": dose_unit,
        "treatment_time_hours": _parse_treatment_hours(assay_text),
        "direction": DIRECTION_BY_CLASS.get(classification, "unknown"),
        "value": value,
        "unit": unit,
        "control_id": None,
        "batch_id": activity.get("record_id") or activity.get("doc_id"),
        "source_url": f"{api_base.rstrip('/')}/activity/{activity.get('activity_id')}.json"
        if activity.get("activity_id")
        else f"{api_base.rstrip('/')}/activity.json",
        "retrieved_at": retrieved_at or _utc_now(),
        "license": license_policy,
        # Provenance extras (not ranking inputs by themselves).
        "activity_id": activity.get("activity_id"),
        "target_chembl_id": activity.get("target_chembl_id"),
        "target_pref_name": activity.get("target_pref_name"),
        "assay_description": activity.get("assay_description"),
        "bao_label": activity.get("bao_label"),
        "pchembl_value": activity.get("pchembl_value"),
        "classification": classification,
        "data_validity_comment": activity.get("data_validity_comment"),
    }
    return record


def _assay_search_priority(assay: dict[str, Any]) -> tuple:
    """Rank assays so HepG2-FFA positive lipid phenotypes fill the snapshot first."""
    desc = str(assay.get("description") or "")
    classification = _classify_chembl_activity(
        {
            "assay_description": desc,
            "bao_label": assay.get("bao_label") or "",
            "target_pref_name": assay.get("target_chembl_id") or "",
        }
    )
    hepg2 = 0 if re.search(r"HepG2", desc, re.I) else 1
    ffa = 0 if _FFA_MODEL_RE.search(desc) else 1
    seed_hepg2_ffa = 0 if str(assay.get("assay_chembl_id") or "") in SEED_HEPG2_FFA_ASSAY_IDS else 1
    return (
        seed_hepg2_ffa,
        _CLASS_PRIORITY.get(classification, 5),
        ffa,
        hepg2,
        assay.get("assay_chembl_id") or "",
    )


def _looks_positive_cellular(assay: dict[str, Any]) -> bool:
    desc = str(assay.get("description") or "")
    return bool(
        LIPID_ENDPOINT_RE.search(desc)
        and POSITIVE_LIPID_DIRECTION_RE.search(desc)
        and CELL_CONTEXT_RE.search(desc)
    )


def _looks_hepg2_ffa(assay_or_row: dict[str, Any]) -> bool:
    desc = str(
        assay_or_row.get("description")
        or assay_or_row.get("assay_description")
        or ""
    )
    return bool(re.search(r"HepG2", desc, re.I) and _FFA_MODEL_RE.search(desc))


def _search_assays(
    get_json: JsonGetter,
    api_base: str,
    terms: Tuple[str, ...],
    *,
    page_limit: int,
    max_assays: int,
    timeout: int,
    scan_per_term: int = 75,
    seed_assay_ids: Optional[Tuple[str, ...]] = None,
) -> tuple[list, list]:
    """Search + paginate, then prefer positive cellular phenotype assays.

    Returns ``(selected_assays, search_errors)``. Term/page failures are isolated
    so a single HTTP 500 does not abort the whole Wave-1 import.
    """
    found: dict[str, dict[str, Any]] = {}
    search_errors: list = []
    for assay_id in seed_assay_ids or ():
        aid = str(assay_id).strip()
        if aid:
            found[aid] = {"assay_chembl_id": aid, "description": "", "seed": True}

    for term in terms:
        offset = 0
        scanned_for_term = 0
        while scanned_for_term < scan_per_term:
            page_size = min(page_limit, scan_per_term - scanned_for_term)
            try:
                data = get_json(
                    f"{api_base.rstrip('/')}/assay.json",
                    {
                        "description__icontains": term,
                        "limit": page_size,
                        "offset": offset,
                    },
                    timeout,
                )
            except Exception as exc:
                search_errors.append(
                    {
                        "term": term,
                        "offset": offset,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
                break
            page = list(data.get("assays") or [])
            if not page:
                break
            for row in page:
                assay_id = str(row.get("assay_chembl_id") or "").strip()
                if assay_id:
                    # Prefer richer assay documents over seed stubs.
                    prev = found.get(assay_id) or {}
                    if prev.get("seed") and not prev.get("description"):
                        found[assay_id] = row
                    elif assay_id not in found:
                        found[assay_id] = row
                    else:
                        found[assay_id] = row
            scanned_for_term += len(page)
            offset += len(page)
            page_meta = data.get("page_meta") or {}
            total = page_meta.get("total_count")
            if total is not None and offset >= int(total):
                break
            if len(page) < page_size:
                break

    ranked = sorted(found.values(), key=_assay_search_priority)
    if not ranked:
        return [], search_errors

    # Soft quota: keep at least half the slot budget for positive cellular hits
    # when available, then fill with remaining ranked assays. Seed IDs always
    # remain eligible even without a description yet (activities carry text).
    positive = [a for a in ranked if _looks_positive_cellular(a) or a.get("seed")]
    others = [a for a in ranked if not (_looks_positive_cellular(a) or a.get("seed"))]
    reserve = max(1, max_assays // 2)
    selected: list[dict[str, Any]] = []
    selected.extend(positive[: max(reserve, max_assays)])
    if len(selected) < max_assays:
        selected.extend(others[: max_assays - len(selected)])
    return selected[:max_assays], search_errors


def _fetch_activities_for_assay(
    get_json: JsonGetter,
    api_base: str,
    assay_id: str,
    *,
    page_limit: int,
    max_activities: int,
    timeout: int,
) -> list[dict[str, Any]]:
    activities: list[dict[str, Any]] = []
    offset = 0
    while len(activities) < max_activities:
        page_size = min(page_limit, max_activities - len(activities))
        data = get_json(
            f"{api_base.rstrip('/')}/activity.json",
            {
                "assay_chembl_id": assay_id,
                "limit": page_size,
                "offset": offset,
            },
            timeout,
        )
        page = list(data.get("activities") or [])
        if not page:
            break
        activities.extend(page)
        page_meta = data.get("page_meta") or {}
        total = page_meta.get("total_count")
        offset += len(page)
        if total is not None and offset >= int(total):
            break
        if len(page) < page_size:
            break
    return activities[:max_activities]


def _reclassify_stored_row(row: dict[str, Any]) -> dict[str, Any]:
    """Refresh classification/direction on a previously stored assay-grain row."""
    updated = dict(row)
    classification = _classify_chembl_activity(
        {
            "assay_description": row.get("assay_description"),
            "target_pref_name": row.get("target_pref_name"),
            "activity_comment": row.get("activity_comment"),
            "data_validity_comment": row.get("data_validity_comment"),
            "standard_type": row.get("endpoint"),
            "bao_label": row.get("bao_label"),
        }
    )
    updated["classification"] = classification
    updated["direction"] = DIRECTION_BY_CLASS.get(classification, "unknown")
    return updated


def _record_dedupe_key(row: dict[str, Any]) -> str:
    activity_id = row.get("activity_id")
    if activity_id is not None:
        return f"activity:{activity_id}"
    return "|".join(
        [
            str(row.get("compound_id") or ""),
            str(row.get("assay_id") or ""),
            str(row.get("endpoint") or ""),
            str(row.get("value") or ""),
        ]
    )


def import_chembl_assay_grain(
    source: dict[str, Any],
    *,
    limit: int = 25,
    page_limit: int = 25,
    max_activities_per_assay: int = 50,
    timeout: int = 60,
    query_terms: Optional[Tuple[str, ...]] = None,
    scan_per_term: int = 75,
    seed_assay_ids: Optional[Tuple[str, ...]] = None,
    merge_existing_path: Optional[Path] = None,
    get_json: Optional[JsonGetter] = None,
    raw_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Search lipid-related assays and emit assay-grain activity records.

    ``limit`` caps the number of distinct assays retained after positive-first
    ranking. Empty / failed searches never become negative labels. Seed assay
    IDs keep positive phenotype expansion available when search is flaky.
    """
    getter = get_json or default_get_json
    api_base = str(source.get("api_base") or "https://www.ebi.ac.uk/chembl/api/data")
    # ``query_terms=()`` is an intentional offline/search-disabled mode used by
    # the seed-driven importer.  Do not treat an explicit empty tuple as
    # "missing" and silently re-enable the flaky free-text assay endpoint.
    terms = DEFAULT_QUERY_TERMS if query_terms is None else tuple(query_terms)
    seeds = seed_assay_ids if seed_assay_ids is not None else SEED_POSITIVE_ASSAY_IDS
    retrieved_at = _utc_now()

    # Status is provenance-only; never let a flaky status endpoint abort import.
    status_payload: Dict[str, Any] = {}
    try:
        if getter is default_get_json:
            status_payload = default_get_json(
                f"{api_base.rstrip('/')}/status.json",
                None,
                timeout=min(15, timeout),
                retries=1,
                backoff_sec=0.5,
            )
        else:
            status_payload = getter(f"{api_base.rstrip('/')}/status.json", None, min(15, timeout))
    except Exception as exc:  # optional provenance
        status_payload = {"status_error": f"{type(exc).__name__}: {exc}"}

    assays, search_errors = _search_assays(
        getter,
        api_base,
        terms,
        page_limit=page_limit,
        max_assays=max(1, limit),
        timeout=timeout,
        scan_per_term=max(page_limit, scan_per_term),
        seed_assay_ids=seeds,
    )
    records_by_key: dict[str, dict[str, Any]] = {}
    raw_pages: list = []
    assay_errors: list = []
    class_counts: Dict[str, int] = {}

    if merge_existing_path is not None and Path(merge_existing_path).is_file():
        for line in Path(merge_existing_path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = _reclassify_stored_row(json.loads(line))
            records_by_key[_record_dedupe_key(row)] = row

    for assay in assays:
        assay_id = str(assay.get("assay_chembl_id") or "")
        try:
            activities = _fetch_activities_for_assay(
                getter,
                api_base,
                assay_id,
                page_limit=page_limit,
                max_activities=max_activities_per_assay,
                timeout=timeout,
            )
        except Exception as exc:
            assay_errors.append(
                {"assay_chembl_id": assay_id, "error_type": type(exc).__name__, "error": str(exc)}
            )
            continue
        raw_pages.append({"assay_chembl_id": assay_id, "activities": activities, "assay": assay})
        for activity in activities:
            row = normalize_chembl_activity_row(
                activity,
                source_id=source["source_id"],
                license_policy=str(source.get("license_policy") or ""),
                api_base=api_base,
                retrieved_at=retrieved_at,
            )
            records_by_key[_record_dedupe_key(row)] = row

    records = list(records_by_key.values())
    for row in records:
        key = str(row.get("classification") or "annotation")
        class_counts[key] = class_counts.get(key, 0) + 1

    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time())
        raw_path = raw_dir / f"chembl_assay_grain_{stamp}.json"
        raw_path.write_text(
            json.dumps(
                {
                    "api_base": api_base,
                    "query_terms": list(terms),
                    "seed_assay_ids": list(seeds or ()),
                    "chembl_status": status_payload,
                    "assay_count": len(assays),
                    "search_errors": search_errors,
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

    # Validate required fields exist on every row (values may be null).
    for record in records:
        missing = [field for field in ASSAY_GRAIN_FIELDS if field not in record]
        if missing:
            raise ValueError(f"assay-grain record missing fields: {missing}")

    if not records:
        raise RuntimeError(
            "chembl assay-grain import produced zero records "
            f"(search_errors={len(search_errors)}, assay_errors={len(assay_errors)}); "
            "refusing to treat network failure as an empty negative table"
        )

    positive_assay_n = sum(
        1 for assay in assays if _looks_positive_cellular(assay) or assay.get("seed")
    )
    hepg2_ffa_rows = sum(
        1
        for row in records
        if row.get("classification") == "positive_phenotype" and _looks_hepg2_ffa(row)
    )
    hepg2_pos_rows = sum(
        1
        for row in records
        if row.get("classification") == "positive_phenotype"
        and re.search(r"HepG2", str(row.get("assay_description") or ""), re.I)
    )
    return {
        "records": records,
        "query_terms": list(terms),
        "seed_assay_ids": list(seeds or ()),
        "seed_hepg2_ffa_assay_ids": list(SEED_HEPG2_FFA_ASSAY_IDS),
        "assay_count": len(assays),
        "positive_cellular_assay_count": positive_assay_n,
        "hepg2_positive_activity_count": hepg2_pos_rows,
        "hepg2_ffa_positive_activity_count": hepg2_ffa_rows,
        "activity_count": len(records),
        "classification_counts": class_counts,
        "scan_per_term": scan_per_term,
        "chembl_release": status_payload.get("chembl_db_version")
        or status_payload.get("chembl_release")
        or status_payload.get("version"),
        "chembl_status": status_payload,
        "api_base": api_base,
        "raw_path": str(raw_path) if raw_path else None,
        "grain": "compound_x_assay_x_activity",
        "assay_errors": assay_errors,
        "search_errors": search_errors,
        "merged_existing": bool(merge_existing_path),
        "note": (
            "Assay-grain public import for prioritization/audit. "
            "Positive cellular lipid phenotypes are preferred when ranking assays. "
            "Database presence and empty searches are not negative labels."
        ),
    }


def resolve_chembl_id_by_inchikey(
    get_json: JsonGetter,
    api_base: str,
    inchikey: str,
    *,
    timeout: int = 60,
) -> Optional[str]:
    """Resolve molecule_chembl_id from a standard InChIKey. Empty ≠ negative."""
    key = (inchikey or "").strip()
    if not key:
        return None
    try:
        payload = get_json(f"{api_base.rstrip('/')}/molecule/{key}.json", None, timeout)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        chembl_id = str(payload.get("molecule_chembl_id") or "").strip()
        if chembl_id:
            return chembl_id
    try:
        search = get_json(
            f"{api_base.rstrip('/')}/molecule.json",
            {"molecule_structures__standard_inchi_key": key, "limit": 1},
            timeout,
        )
    except Exception:
        return None
    molecules = list((search or {}).get("molecules") or [])
    if not molecules:
        return None
    return str(molecules[0].get("molecule_chembl_id") or "").strip() or None


def _fetch_activities_for_molecule(
    get_json: JsonGetter,
    api_base: str,
    molecule_chembl_id: str,
    *,
    page_limit: int,
    max_activities: int,
    timeout: int,
) -> list[dict[str, Any]]:
    activities: list[dict[str, Any]] = []
    offset = 0
    while len(activities) < max_activities:
        page_size = min(page_limit, max_activities - len(activities))
        data = get_json(
            f"{api_base.rstrip('/')}/activity.json",
            {
                "molecule_chembl_id": molecule_chembl_id,
                "limit": page_size,
                "offset": offset,
            },
            timeout,
        )
        page = list(data.get("activities") or [])
        if not page:
            break
        activities.extend(page)
        page_meta = data.get("page_meta") or {}
        total = page_meta.get("total_count")
        offset += len(page)
        if total is not None and offset >= int(total):
            break
        if len(page) < page_size:
            break
    return activities[:max_activities]


def import_chembl_by_inchikeys(
    source: dict[str, Any],
    inchikeys: Sequence[str],
    *,
    max_activities_per_molecule: int = 40,
    page_limit: int = 25,
    timeout: int = 60,
    merge_existing_path: Optional[Path] = None,
    get_json: Optional[JsonGetter] = None,
    raw_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Candidate-level ChEMBL expansion: exact InChIKey → molecule → activities.

    Complements assay-seed import so Top-M / shortlist compounds enter the public
    assay-grain index. Missing molecules stay ``verified_empty`` and never become
    inactive or low-toxicity labels.
    """
    getter = get_json or default_get_json
    api_base = str(source.get("api_base") or "https://www.ebi.ac.uk/chembl/api/data")
    retrieved_at = _utc_now()
    # Injected test getters must remain isolated; production calls reuse the
    # shared local-first state across SDF imports.
    query_cache = (
        EvidenceQueryCache(ROOT / "data/public/cache/evidence_query_state.sqlite")
        if get_json is None
        else None
    )
    wanted = []
    for item in inchikeys:
        key = str(item or "").strip()
        if key and key not in wanted:
            wanted.append(key)

    records_by_key: dict[str, dict[str, Any]] = {}
    if merge_existing_path is not None and Path(merge_existing_path).is_file():
        for line in Path(merge_existing_path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = _reclassify_stored_row(json.loads(line))
            records_by_key[_record_dedupe_key(row)] = row

    molecule_stats: list[dict[str, Any]] = []
    molecule_errors: list[dict[str, Any]] = []
    raw_pages: list = []
    class_counts: Dict[str, int] = {}
    resolved = 0
    verified_empty = 0

    for inchikey in wanted:
        cached_payload: dict[str, Any] | None = None
        if query_cache is not None:
            query_cache.upsert_entity(inchikey, original_inchikey=inchikey)
            decision = query_cache.decide(
                source_id="chembl",
                entity_key=inchikey,
                endpoint="molecule_activity",
                online=True,
            )
            if decision.action == "skip_fresh_verified_empty":
                verified_empty += 1
                molecule_stats.append(
                    {
                        "inchikey": inchikey,
                        "status": "verified_empty",
                        "molecule_chembl_id": None,
                        "activity_count": 0,
                        "cache_action": decision.action,
                    }
                )
                continue
            if decision.action == "local_hit":
                payload = query_cache.load_payload(
                    source_id="chembl",
                    entity_key=inchikey,
                    endpoint="molecule_activity",
                )
                if isinstance(payload, dict):
                    cached_payload = payload
        try:
            mode = "cache" if cached_payload is not None else "live"
            chembl_id = (
                cached_payload.get("molecule_chembl_id")
                if cached_payload is not None
                else resolve_chembl_id_by_inchikey(getter, api_base, inchikey, timeout=timeout)
            )
            if not chembl_id:
                verified_empty += 1
                molecule_stats.append(
                    {
                        "inchikey": inchikey,
                        "status": "verified_empty",
                        "molecule_chembl_id": None,
                        "activity_count": 0,
                        "cache_action": "local_hit" if cached_payload is not None else None,
                    }
                )
                if query_cache is not None and cached_payload is None:
                    query_cache.record(
                        source_id="chembl",
                        entity_key=inchikey,
                        endpoint="molecule_activity",
                        status="verified_empty",
                        ttl=timedelta(days=14),
                        source_version="chembl-api",
                    )
                continue
            activities = (
                cached_payload.get("activities", [])
                if cached_payload is not None
                else _fetch_activities_for_molecule(
                    getter,
                    api_base,
                    chembl_id,
                    page_limit=page_limit,
                    max_activities=max_activities_per_molecule,
                    timeout=timeout,
                )
            )
            resolved += 1
            raw_pages.append(
                {
                    "inchikey": inchikey,
                    "molecule_chembl_id": chembl_id,
                    "activities": activities,
                    "mode": mode,
                }
            )
            if query_cache is not None and cached_payload is None:
                query_cache.record(
                    source_id="chembl",
                    entity_key=inchikey,
                    endpoint="molecule_activity",
                    status="hit",
                    ttl=timedelta(days=90),
                    payload={"molecule_chembl_id": chembl_id, "activities": activities},
                    source_version="chembl-api",
                )
            kept = 0
            for activity in activities:
                row = normalize_chembl_activity_row(
                    activity,
                    source_id=source["source_id"],
                    license_policy=str(source.get("license_policy") or ""),
                    api_base=api_base,
                    retrieved_at=retrieved_at,
                )
                row["inchikey"] = inchikey
                records_by_key[_record_dedupe_key(row)] = row
                kept += 1
            molecule_stats.append(
                {
                    "inchikey": inchikey,
                    "status": "exact_hit",
                    "molecule_chembl_id": chembl_id,
                    "activity_count": kept,
                    "cache_action": "local_hit" if cached_payload is not None else None,
                }
            )
        except Exception as exc:
            if query_cache is not None:
                query_cache.record(
                    source_id="chembl",
                    entity_key=inchikey,
                    endpoint="molecule_activity",
                    status="query_failed",
                    retry_after=timedelta(hours=1),
                    error=exc,
                    source_version="chembl-api",
                )
            molecule_errors.append(
                {
                    "inchikey": inchikey,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

    records = list(records_by_key.values())
    for row in records:
        key = str(row.get("classification") or "annotation")
        class_counts[key] = class_counts.get(key, 0) + 1

    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time())
        raw_path = raw_dir / f"chembl_by_inchikey_{stamp}.json"
        raw_path.write_text(
            json.dumps(
                {
                    "api_base": api_base,
                    "inchikeys": wanted,
                    "molecule_stats": molecule_stats,
                    "molecule_errors": molecule_errors,
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

    if query_cache is not None:
        query_cache.close()

    for record in records:
        missing = [field for field in ASSAY_GRAIN_FIELDS if field not in record]
        if missing:
            raise ValueError(f"assay-grain record missing fields: {missing}")

    if not records and not wanted:
        raise RuntimeError("chembl by-inchikey import received an empty InChIKey list")

    hepg2_ffa_rows = sum(
        1
        for row in records
        if row.get("classification") == "positive_phenotype" and _looks_hepg2_ffa(row)
    )
    return {
        "records": records,
        "inchikeys_requested": wanted,
        "inchikeys_resolved": resolved,
        "inchikeys_verified_empty": verified_empty,
        "activity_count": len(records),
        "classification_counts": class_counts,
        "hepg2_ffa_positive_activity_count": hepg2_ffa_rows,
        "molecule_stats": molecule_stats,
        "molecule_errors": molecule_errors,
        "api_base": api_base,
        "raw_path": str(raw_path) if raw_path else None,
        "grain": "compound_x_assay_x_activity",
        "merged_existing": bool(merge_existing_path),
        "note": (
            "Candidate-level ChEMBL expansion by exact InChIKey. "
            "Presence without phenotype stays annotation; empty search is not a negative label."
        ),
    }
