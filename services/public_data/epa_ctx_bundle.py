"""Candidate-scoped EPA CTX chemical, bioactivity and hazard evidence client."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from services.evidence_gateway.credentials import resolve_secret

CTX_BASE = "https://comptox.epa.gov/ctx-api"
ENDPOINTS = {
    "chemical_detail": "chemical/detail/search/by-dtxsid/{dtxsid}?projection=chemicaldetailall",
    "bioactivity_summary": "bioactivity/data/summary/search/by-dtxsid/{dtxsid}",
    "bioactivity_detail": "bioactivity/data/search/by-dtxsid/{dtxsid}",
    "toxval": "hazard/toxval/search/by-dtxsid/{dtxsid}",
    "toxref_summary": "hazard/toxref/summary/search/by-dtxsid/{dtxsid}",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class CtxClient:
    def __init__(self, api_key: str | None = None, *, timeout: int = 45, retries: int = 3):
        self.api_key = resolve_secret(
            "epa_ctx",
            explicit=api_key,
            env_names=("CTX_API_KEY", "CCTE_API_KEY", "MOLMIND_CTX_API_KEY"),
        ) or ""
        if not self.api_key:
            raise ValueError("CTX_API_KEY is required")
        self.timeout = timeout
        self.retries = retries

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{CTX_BASE}/{path.lstrip('/')}"
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        request = Request(url, headers={
            "x-api-key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "MolMind-CTX-import/1.0",
        })
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - fixed EPA base
                    raw = response.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else []
            except HTTPError as exc:
                # Client/auth errors are deterministic for this request. Retrying
                # them only multiplies API load and delays the CAS fallback.
                if exc.code in {400, 401, 403, 404}:
                    raise
                last = exc
                if attempt + 1 < self.retries:
                    time.sleep(1.5 * (attempt + 1))
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = exc
                if attempt + 1 < self.retries:
                    time.sleep(1.5 * (attempt + 1))
        assert last is not None
        raise last

    def search_exact(self, value: str) -> list[dict[str, Any]]:
        payload = self.get_json(
            f"chemical/search/equal/{quote(value, safe='')}",
            {"projection": "chemicalsearchall"},
        )
        return payload if isinstance(payload, list) else []


def map_candidate(client: CtxClient, candidate: dict[str, Any]) -> dict[str, Any]:
    """Map a candidate to DTXSID.

    Prefer InChIKey bases before CAS so stage-2 scoring can use exact identity.
    ``standardized_inchikey`` hits count as exact (pipeline-canonical structure).
    """
    attempts = [
        ("original_inchikey", candidate.get("original_inchikey")),
        ("standardized_inchikey", candidate.get("standardized_inchikey")),
        ("cas", candidate.get("cas")),
    ]
    errors: list[dict[str, str]] = []
    for basis, value in attempts:
        if not value:
            continue
        # Skip duplicate standardized key when it equals original.
        if (
            basis == "standardized_inchikey"
            and value == candidate.get("original_inchikey")
        ):
            continue
        try:
            hits = client.search_exact(str(value))
        except Exception as exc:
            errors.append({"basis": basis, "error_type": type(exc).__name__, "error": str(exc)})
            continue
        if hits:
            hit = hits[0]
            exact = basis in {"original_inchikey", "standardized_inchikey"}
            return {
                "molecule_id": candidate.get("molecule_id"),
                "dtxsid": hit.get("dtxsid"),
                "dtxcid": hit.get("dtxcid"),
                "preferred_name": hit.get("preferredName"),
                "casrn": hit.get("casrn"),
                "ctx_smiles": hit.get("smiles"),
                "mapping_status": (
                    "exact_identifier_match"
                    if exact
                    else "identifier_match_requires_structure_audit"
                ),
                "mapping_basis": basis,
                "mapping_value": value,
                "hit_count": len(hits),
                "retrieved_at": utc_now(),
                "errors": errors,
            }
    return {
        "molecule_id": candidate.get("molecule_id"),
        "dtxsid": None,
        "mapping_status": "audit_missing",
        "mapping_basis": None,
        "retrieved_at": utc_now(),
        "errors": errors,
    }


def query_candidate(client: CtxClient, mapping: dict[str, Any]) -> dict[str, Any]:
    dtxsid = mapping.get("dtxsid")
    if not dtxsid:
        return {"mapping": mapping, "responses": {}, "errors": []}
    responses: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    for name, template in ENDPOINTS.items():
        try:
            responses[name] = client.get_json(template.format(dtxsid=dtxsid))
        except Exception as exc:
            errors.append({"endpoint": name, "error_type": type(exc).__name__, "error": str(exc)})
    return {"mapping": mapping, "responses": responses, "errors": errors}


def response_count(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        return 1 if payload else 0
    return 0
