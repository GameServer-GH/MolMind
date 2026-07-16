"""EPA ToxCast/Tox21 via CTX Bioactivity API (DTXSID lookups).

Figshare invitrodb dumps are GB-scale and currently HTTP 403. This module pulls
per-chemical bioactivity from CTX and keeps only assay-grain risk signals.
Missing API key / empty search / network failure never become “safe” or
“inactive-as-negative” labels.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

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

DEFAULT_BIOACTIVITY_API = "https://comptox.epa.gov/ctx-api/bioactivity"
DEFAULT_CHEMICAL_API = "https://comptox.epa.gov/ctx-api/chemical"

# Curated DTXSID seed set for Wave-2 snapshot when candidate mapping is absent.
# Used for audit/mechanism risk coverage — not a claim of HepG2-FFA relevance.
DEFAULT_SEED_DTXSID = (
    "DTXSID7020182",  # Bisphenol A
    "DTXSID3020966",  # Acetaminophen
    "DTXSID2021300",  # Aspirin / Acetylsalicylic acid
    "DTXSID9044297",  # Metformin
    "DTXSID2045232",  # Tiratricol (docs example)
    "DTXSID30944145",  # Mianserin HCl (docs example)
)

HITC_ACTIVE_THRESHOLD = 0.9
JsonGetter = Callable[[str, Optional[Dict[str, str]], int], Any]

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE_DIR = ROOT / "data" / "public" / "fixtures" / "toxcast_ctx"


class AuthMissingError(RuntimeError):
    """CTX API key is required for live lookups."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def resolve_api_key(explicit: Optional[str] = None) -> Optional[str]:
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    for env_name in ("CTX_API_KEY", "CCTE_API_KEY", "MOLMIND_CTX_API_KEY"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    return None


def default_get_json(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 60,
    *,
    retries: int = 2,
    backoff_sec: float = 1.5,
) -> Any:
    last_error: Optional[Exception] = None
    hdrs = {
        "User-Agent": "MolMind-public-import/1.1",
        "Accept": "application/json",
        **(headers or {}),
    }
    for attempt in range(max(1, retries)):
        try:
            request = Request(url, headers=hdrs)
            with urlopen(request, timeout=timeout) as response:  # noqa: S310
                raw = response.read().decode("utf-8", errors="replace").strip()
            if not raw:
                return []
            return json.loads(raw)
        except HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                body = str(exc)
            if exc.code in {401, 403}:
                raise AuthMissingError(f"CTX HTTP {exc.code}: {body}") from exc
            last_error = RuntimeError(f"HTTP {exc.code}: {body}")
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt + 1 >= retries:
            break
        time.sleep(backoff_sec * (attempt + 1))
    assert last_error is not None
    raise last_error


def _as_row_list(payload: Any) -> List[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "items"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return [row for row in nested if isinstance(row, dict)]
        return [payload]
    return []


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_toxcast_row(
    row: dict[str, Any],
    *,
    source_id: str,
    license_policy: str,
    api_base: str,
    retrieved_at: Optional[str] = None,
) -> Dict[str, Any]:
    dtxsid = str(row.get("dtxsid") or "").strip()
    aeid = row.get("aeid")
    hitc = _float_or_none(row.get("hitc") if row.get("hitc") is not None else row.get("hitcall"))
    ac50 = _float_or_none(row.get("ac50"))
    endpoint = (
        str(row.get("assayComponentEndpointName") or row.get("assay_name") or "").strip()
        or (f"aeid:{aeid}" if aeid is not None else "toxcast_endpoint")
    )
    active = hitc is not None and hitc >= HITC_ACTIVE_THRESHOLD
    # Inactive/non-hit is not a safety clearance label.
    direction = "risk" if active else "unknown"
    record = {
        "compound_id": dtxsid or None,
        "standardized_smiles": row.get("smiles") or row.get("canonicalSmiles"),
        "inchikey": row.get("inchikey") or row.get("inchiKey"),
        "cas": row.get("casn") or row.get("casrn"),
        "compound_name": row.get("chnm") or row.get("preferredName"),
        "source_id": source_id,
        "assay_id": f"ToxCast:aeid:{aeid}" if aeid is not None else f"ToxCast:{endpoint}",
        "endpoint": endpoint,
        "dose": None,
        "dose_unit": None,
        "treatment_time_hours": _float_or_none(row.get("timepointHr")),
        "direction": direction,
        "value": ac50,
        "unit": "uM" if ac50 is not None else None,
        "control_id": None,
        "batch_id": row.get("m4id") or row.get("m5id") or row.get("spid"),
        "source_url": f"{api_base.rstrip('/')}/data/search/by-dtxsid/{quote(dtxsid)}"
        if dtxsid
        else api_base,
        "retrieved_at": retrieved_at or _utc_now(),
        "license": license_policy,
        "dtxsid": dtxsid or None,
        "aeid": aeid,
        "hitc": hitc,
        "hitcall": _float_or_none(row.get("hitcall")),
        "ac50": ac50,
        "cell_viability_assay": row.get("cellViabilityAssay"),
        "intended_target_family": row.get("intendedTargetFamily"),
        "classification": "active_risk" if active else "inactive_or_inconclusive",
        "evidence_role": "risk_signal" if active else "annotation_only",
        "molmind_direction": direction,
        "active_hit": active,
    }
    return record


def _record_key(row: dict[str, Any]) -> str:
    return "|".join(
        [
            str(row.get("compound_id") or ""),
            str(row.get("assay_id") or ""),
            str(row.get("batch_id") or ""),
            str(row.get("value") or ""),
        ]
    )


def fetch_bioactivity_by_dtxsid(
    dtxsid: str,
    *,
    api_key: str,
    api_base: str = DEFAULT_BIOACTIVITY_API,
    timeout: int = 60,
    get_json: Optional[JsonGetter] = None,
) -> List[dict[str, Any]]:
    getter = get_json or default_get_json
    url = f"{api_base.rstrip('/')}/data/search/by-dtxsid/{quote(dtxsid)}"
    payload = getter(url, {"x-api-key": api_key}, timeout)
    return _as_row_list(payload)


def resolve_dtxsid_by_inchikey(
    inchikey: str,
    *,
    api_key: str,
    api_base: str = DEFAULT_CHEMICAL_API,
    timeout: int = 45,
    get_json: Optional[JsonGetter] = None,
) -> Optional[str]:
    getter = get_json or default_get_json
    url = f"{api_base.rstrip('/')}/search/equal/{quote(inchikey)}"
    payload = getter(url, {"x-api-key": api_key}, timeout)
    rows = _as_row_list(payload)
    if not rows and isinstance(payload, dict):
        rows = [payload]
    for row in rows:
        dtxsid = str(row.get("dtxsid") or row.get("DTXSID") or "").strip()
        if dtxsid.startswith("DTXSID"):
            return dtxsid
    return None


def import_toxcast_ctx(
    source: dict[str, Any],
    *,
    limit: int = 80,
    per_dtxsid_limit: int = 20,
    dtxsids: Optional[Sequence[str]] = None,
    inchikeys: Optional[Sequence[str]] = None,
    api_key: Optional[str] = None,
    timeout: int = 60,
    cache_dir: Optional[Path] = None,
    fixture_dir: Optional[Path] = None,
    get_json: Optional[JsonGetter] = None,
    raw_dir: Optional[Path] = None,
    allow_fixture_fallback: bool = True,
) -> Dict[str, Any]:
    """Import ToxCast assay-grain rows by DTXSID via CTX.

    Preference order per DTXSID: local cache → live CTX (needs key) → fixture.
    """
    bio_api = str(source.get("api_base") or DEFAULT_BIOACTIVITY_API)
    chem_api = str(source.get("chemical_api_base") or DEFAULT_CHEMICAL_API)
    key = resolve_api_key(api_key)
    retrieved_at = _utc_now()
    fixture_dir = Path(fixture_dir) if fixture_dir else DEFAULT_FIXTURE_DIR
    cache_dir_path = Path(cache_dir) if cache_dir else None

    wanted: list[str] = []
    for item in dtxsids or ():
        text = str(item).strip()
        if text and text not in wanted:
            wanted.append(text)

    resolve_errors: list[dict[str, Any]] = []
    if inchikeys:
        if not key:
            resolve_errors.append(
                {
                    "error_type": "AuthMissingError",
                    "error": "InChIKey→DTXSID resolution requires CTX_API_KEY",
                }
            )
        else:
            for inchikey in inchikeys:
                try:
                    dtxsid = resolve_dtxsid_by_inchikey(
                        inchikey,
                        api_key=key,
                        api_base=chem_api,
                        timeout=timeout,
                        get_json=get_json,
                    )
                    if dtxsid and dtxsid not in wanted:
                        wanted.append(dtxsid)
                    elif not dtxsid:
                        resolve_errors.append(
                            {"inchikey": inchikey, "status": "verified_empty"}
                        )
                except Exception as exc:
                    resolve_errors.append(
                        {
                            "inchikey": inchikey,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                    )

    if not wanted:
        wanted = list(DEFAULT_SEED_DTXSID)

    records_by_key: dict[str, dict[str, Any]] = {}
    dtxsid_stats: list[dict[str, Any]] = []
    dtxsid_errors: list[dict[str, Any]] = []
    mode_counts = {"cache": 0, "live": 0, "fixture": 0}

    for dtxsid in wanted:
        if len(records_by_key) >= limit:
            break
        payload: Any = None
        mode = ""
        cache_path = cache_dir_path / f"{dtxsid}.json" if cache_dir_path else None
        fixture_path = fixture_dir / f"{dtxsid}.json"
        try:
            if cache_path is not None and cache_path.is_file():
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                mode = "cache"
            elif key:
                payload = fetch_bioactivity_by_dtxsid(
                    dtxsid,
                    api_key=key,
                    api_base=bio_api,
                    timeout=timeout,
                    get_json=get_json,
                )
                mode = "live"
                if cache_dir_path is not None:
                    cache_dir_path.mkdir(parents=True, exist_ok=True)
                    (cache_dir_path / f"{dtxsid}.json").write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
            elif allow_fixture_fallback and fixture_path.is_file():
                payload = json.loads(fixture_path.read_text(encoding="utf-8"))
                mode = "fixture"
            else:
                raise AuthMissingError(
                    "CTX_API_KEY unset and no cache/fixture for "
                    f"{dtxsid}; request a free key from ccte_api@epa.gov"
                )
        except Exception as exc:
            dtxsid_errors.append(
                {
                    "dtxsid": dtxsid,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            continue

        mode_counts[mode] = mode_counts.get(mode, 0) + 1
        rows = _as_row_list(payload)
        kept = 0
        for row in rows:
            if kept >= per_dtxsid_limit or len(records_by_key) >= limit:
                break
            # Ensure dtxsid present on sparse fixture rows.
            if not row.get("dtxsid"):
                row = {**row, "dtxsid": dtxsid}
            record = normalize_toxcast_row(
                row,
                source_id=source["source_id"],
                license_policy=str(source.get("license_policy") or ""),
                api_base=bio_api,
                retrieved_at=retrieved_at,
            )
            if not record.get("compound_id") or not record.get("assay_id"):
                continue
            records_by_key[_record_key(record)] = record
            kept += 1
        dtxsid_stats.append(
            {
                "dtxsid": dtxsid,
                "returned": len(rows),
                "kept": kept,
                "mode": mode,
                "active_hits": sum(
                    1
                    for r in list(records_by_key.values())[-kept:]
                    if r.get("active_hit")
                ),
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
        raw_path = raw_dir / f"toxcast_ctx_{stamp}.json"
        raw_path.write_text(
            json.dumps(
                {
                    "bioactivity_api": bio_api,
                    "chemical_api": chem_api,
                    "seed_dtxsids": list(wanted),
                    "mode_counts": mode_counts,
                    "dtxsid_stats": dtxsid_stats,
                    "dtxsid_errors": dtxsid_errors,
                    "resolve_errors": resolve_errors,
                    "api_key_present": bool(key),
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
        if not key and mode_counts.get("fixture", 0) == 0 and mode_counts.get("cache", 0) == 0:
            raise AuthMissingError(
                "CTX_API_KEY unset; no ToxCast cache/fixture rows available. "
                "Email ccte_api@epa.gov for a free key, or provide --toxcast-cache."
            )
        raise RuntimeError(
            "toxcast CTX import produced zero records "
            f"(errors={len(dtxsid_errors)}); refusing empty negative table"
        )

    active_n = sum(1 for row in records if row.get("active_hit"))
    return {
        "records": records,
        "activity_count": len(records),
        "active_hit_count": active_n,
        "dtxsid_count": len(dtxsid_stats),
        "dtxsid_stats": dtxsid_stats,
        "dtxsid_errors": dtxsid_errors,
        "resolve_errors": resolve_errors,
        "mode_counts": mode_counts,
        "api_key_present": bool(key),
        "hitc_active_threshold": HITC_ACTIVE_THRESHOLD,
        "api_base": bio_api,
        "raw_path": str(raw_path) if raw_path else None,
        "grain": "compound_x_assay_x_activity",
        "ranking_effect": "risk_signal_only",
        "dataset_version": "ToxCast via CTX Bioactivity API",
        "note": (
            "CTX DTXSID bioactivity subset. Active hits (hitc>=threshold) are "
            "risk_signal only; inactive/non-hit is never a safety clearance. "
            "Figshare invitrodb full dump is intentionally not shipped."
        ),
    }
