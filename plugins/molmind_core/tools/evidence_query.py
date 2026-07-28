"""Read-only, local-first candidate evidence query orchestration.

This module deliberately does not call the ranker and never appends live
responses to a scoring snapshot.  It resolves one molecular identity, reads
the frozen/local facade, consults the gateway query-state cache, and only then
allows explicitly requested provider enrichment.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote

import httpx

from packages.models import EvidenceHit
from plugins.molmind_core.scientific.evidence_facade.bundle import (
    EvidenceBundle,
    infer_evidence_type,
)
from plugins.molmind_core.scientific.evidence_facade.facade import EvidenceFacade
from plugins.molmind_core.scientific.evidence_gateway.cache import EvidenceQueryCache
from plugins.molmind_core.scientific.evidence_gateway.contract import (
    STATUS_PRIORITY,
    canonical_status as gateway_canonical_status,
    claim_ceiling as gateway_claim_ceiling,
    content_sha256,
    json_safe,
    lookup_field_family,
    lookup_value_equal,
    redact_text,
)
from plugins.molmind_core.scientific.evidence_gateway.credentials import resolve_secret
from plugins.molmind_core.scientific.evidence_gateway.identity import (
    IdentityResolution,
    resolve_identity,
    resolution_from_mapping,
)
from plugins.molmind_core.scientific.evidence_gateway.retriever import (
    EvidenceRetriever,
    load_provider_config,
)
from plugins.molmind_core.scientific.paths import REPO_ROOT
from plugins.molmind_core.scientific.pipeline import load_config
from plugins.molmind_core.scientific.pipeline.config_loader import SNAPSHOT_DIR
from plugins.molmind_core.scientific.public_data.epa_ctx_bundle import CtxClient, query_candidate
from plugins.molmind_core.scientific.evidence_facade.epa_risk import (
    epa_cytotox_metrics,
    epa_cytotox_risk_tier,
)

EventSink = Callable[[dict[str, Any]], None]
ProviderAdapter = Callable[[Any], list[EvidenceHit]]

_STATUS_PRIORITY = STATUS_PRIORITY
_PROVIDER_ALIASES = {
    "epa": "epa_ctx",
    "toxcast": "epa_ctx",
    "tox21": "epa_ctx",
    "chembl_lipid_v1": "chembl",
    "pubchem_tox_v1": "pubchem",
}
def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _redact_text(value: Any) -> str:
    return redact_text(value)


def _redact_value(value: Any) -> Any:
    return json_safe(value)


def _sha256(value: Any) -> str:
    return content_sha256(value)


def _emit(sink: EventSink | None, event: dict[str, Any]) -> None:
    if sink is None:
        return
    try:
        sink(dict(event))
    except Exception:
        # UI/event persistence is observational and must not change retrieval.
        return


class _ProviderRequestGate:
    """Thread-safe minimum interval applied to each real HTTP subrequest."""

    def __init__(self, rate_per_sec: object):
        try:
            rate = max(0.0, float(rate_per_sec or 0.0))
        except (TypeError, ValueError):
            rate = 0.0
        self._interval = 1.0 / rate if rate > 0 else 0.0
        self._next_allowed = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            target = max(now, self._next_allowed)
            self._next_allowed = target + self._interval
        delay = target - now
        if delay > 0:
            time.sleep(delay)


def _canonical_status(value: object, hit: EvidenceHit | None = None) -> str:
    return gateway_canonical_status(value, hit)


def _provider_id(adapter_id: str, explicit: str = "") -> str:
    if explicit:
        return _PROVIDER_ALIASES.get(explicit.lower(), explicit.lower())
    value = str(adapter_id or "").lower()
    if "chembl" in value:
        return "chembl"
    if "pubchem" in value:
        return "pubchem"
    if "bindingdb" in value:
        return "bindingdb"
    if any(token in value for token in ("epa", "toxcast", "tox21", "ctx")):
        return "epa_ctx"
    if "dilirank" in value:
        return "dilirank"
    if "nafld" in value:
        return "nafldkb"
    return value or "local"


def _identity_accession(hit: EvidenceHit) -> str:
    """Return a provider-specific compound identity, never an assay id."""

    payload = hit.payload or {}
    provider = _provider_id(hit.adapter_id, hit.provider_id)
    keys = {
        "epa_ctx": ("dtxsid",),
        "pubchem": ("cid",),
        "chembl": ("chembl_id", "molecule_chembl_id"),
    }.get(provider, ())
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            text = str(value).strip()
            if key == "cid" and text.isdigit():
                text = str(int(text))
            elif key in {"dtxsid", "chembl_id", "molecule_chembl_id"}:
                text = text.upper()
            return f"{key}:{text}"
    return ""


def _lookup_field_family(value: str) -> str:
    return lookup_field_family(value)


def _lookup_value_equal(field: str, left: str, right: str) -> bool:
    return lookup_value_equal(field, left, right)


def _claim_ceiling(hit: EvidenceHit) -> str:
    return gateway_claim_ceiling(hit)


def _lookup_for_provider(
    resolution: IdentityResolution,
    provider_id: str,
    provider_configs: Mapping[str, Mapping[str, Any]],
) -> tuple[str, str, str]:
    config = provider_configs.get(provider_id) or {}
    order = config.get("identity_order") or (
        "original_inchikey",
        "standardized_inchikey",
        "cas",
        "standardized_smiles",
    )
    field, value, match_type = resolution.lookup_for(order)
    return field or "", value or "", match_type or ""


def _normalize_hit(
    hit: EvidenceHit,
    *,
    resolution: IdentityResolution,
    provider_configs: Mapping[str, Mapping[str, Any]],
    provider_hint: str = "",
    lookup: tuple[str, str, str] | None = None,
) -> EvidenceHit:
    hit.raw_status = hit.raw_status or str(hit.query_status or "")
    hit.payload = _redact_value(hit.payload or {})
    hit.query_params = _redact_value(hit.query_params or {})
    hit.source_url = _redact_text(hit.source_url)
    provider = _provider_id(hit.adapter_id, hit.provider_id or provider_hint)
    field, value, match_type = lookup or _lookup_for_provider(
        resolution, provider, provider_configs
    )
    reported_field = str(hit.lookup_field or "").strip()
    reported_value = str(hit.lookup_value or "").strip()
    field_conflict = bool(
        reported_field
        and field
        and _lookup_field_family(reported_field) != _lookup_field_family(field)
    )
    value_conflict = bool(
        reported_value
        and value
        and not _lookup_value_equal(field or reported_field, reported_value, value)
    )
    if field_conflict or value_conflict:
        original_payload = dict(hit.payload or {})
        hit.payload = {
            "reason": "provider_lookup_metadata_conflict",
            "planned_lookup_field": field,
            "planned_lookup_value": value,
            "reported_lookup_field": reported_field,
            "reported_lookup_value": reported_value,
            "provider_payload": original_payload,
        }
        hit.query_type = "query_audit"
        hit.evidence_role = "query_audit"
        hit.query_status = "identity_review_required"
        hit.evidence_type = "query_audit"
        hit.score = 0.0
        hit.confidence = 0.0
        hit.direction = "unknown"
        hit.endpoint = "provider_lookup_identity_validation"
        hit.claim_ceiling = "identity_review_only_no_efficacy_or_safety_claim"
        hit.evidence_id = (
            f"{provider}:identity_review:"
            f"{_sha256([field, value, reported_field, reported_value])[:16]}"
        )
    hit.provider_id = provider
    canonical_status = _canonical_status(hit.query_status, hit)
    if canonical_status == "annotation_only" or hit.evidence_role == "annotation_only":
        hit.query_status = "annotation_only"
        hit.evidence_role = "annotation_only"
        hit.score = 0.0
        hit.confidence = 0.0
    elif canonical_status in {
        "verified_empty",
        "query_failed",
        "auth_missing",
        "not_queried",
        "identity_review_required",
    }:
        if hit.evidence_role != "query_audit":
            hit.payload = {
                "reason": "non_hit_status_cannot_be_task_evidence",
                "provider_query_status": canonical_status,
                "provider_payload": dict(hit.payload or {}),
            }
        hit.query_status = canonical_status  # type: ignore[assignment]
        hit.query_type = "query_audit"
        hit.evidence_role = "query_audit"
        hit.evidence_type = "query_audit"
        hit.score = 0.0
        hit.confidence = 0.0
        hit.direction = "unknown"
    else:
        hit.query_status = "hit"
    hit.lookup_field = field or reported_field
    hit.lookup_value = value or reported_value
    hit.match_type = (
        "provider_lookup_metadata_conflict"
        if field_conflict or value_conflict
        else (hit.match_type or match_type)
    )
    hit.evidence_type = infer_evidence_type(hit)  # type: ignore[assignment]
    hit.adapter_version = hit.adapter_version or hit.adapter_id or provider
    hit.source_version = hit.source_version or hit.adapter_version
    hit.claim_ceiling = _claim_ceiling(hit)
    if not hit.endpoint:
        hit.endpoint = f"{hit.query_type or 'evidence'}_record"
        hit.payload = {
            **(hit.payload or {}),
            "endpoint_basis": "legacy_record_missing_endpoint",
        }
    if not hit.accession:
        payload = hit.payload or {}
        for key in ("accession", "chembl_id", "cid", "dtxsid", "assay_id"):
            if payload.get(key) not in (None, ""):
                hit.accession = str(payload[key])
                break
    if not hit.retrieved_at:
        hit.retrieved_at = _now()
        hit.payload = {
            **(hit.payload or {}),
            "retrieved_at_basis": "local_query_time_source_timestamp_missing",
        }
    if not hit.response_sha256:
        # Some legacy frozen rows predate raw-response hashes.  Hash the
        # normalized evidence payload, and label that basis instead of
        # pretending it is the provider's original response body.
        hit.response_sha256 = _sha256(hit.payload or {})
        hit.payload = {
            **(hit.payload or {}),
            "response_sha256_basis": "normalized_evidence_payload",
        }
    if not hit.evidence_id:
        hit.evidence_id = (
            f"{provider}:{hit.query_type}:"
            f"{_sha256([hit.lookup_field, hit.lookup_value, hit.endpoint, hit.payload])[:16]}"
        )
    if not hit.source_url and not hit.accession:
        hit.accession = hit.evidence_id
        hit.payload = {
            **(hit.payload or {}),
            "accession_basis": "evidence_id_fallback_source_locator",
        }
    if hit.evidence_role in {"query_audit", "annotation_only"}:
        hit.score = 0.0
        hit.confidence = 0.0
    if hit.query_status == "identity_review_required":
        hit.query_type = "query_audit"
        hit.evidence_role = "query_audit"
        hit.evidence_type = "query_audit"
        hit.score = 0.0
        hit.confidence = 0.0
    return hit


def _add_hit(bundle: EvidenceBundle, hit: EvidenceHit) -> None:
    if hit.query_status == "identity_review_required" or hit.query_type == "query_audit":
        target = bundle.query_audit
    elif hit.query_type == "lipid":
        target = bundle.lipid
    elif hit.query_type == "tox":
        target = bundle.tox
    elif hit.query_type == "novelty":
        target = bundle.novelty
    elif hit.query_type == "pathway":
        target = bundle.pathway
    else:
        target = bundle.annotation
    if not any(existing.evidence_id == hit.evidence_id for existing in bundle.all_hits()):
        target.append(hit)


def _audit_hit(
    *,
    status: str,
    resolution: IdentityResolution,
    provider_id: str = "identity_resolver",
    reason: str,
) -> EvidenceHit:
    field, value, match_type = (
        resolution.lookup_field,
        resolution.lookup_value,
        resolution.match_type,
    )
    payload = {
        "reason": reason,
        "identity_status": resolution.status,
        "conflicts": list(resolution.conflicts),
    }
    return EvidenceHit(
        adapter_id=provider_id,
        provider_id=provider_id,
        query_type="query_audit",
        score=0.0,
        confidence=0.0,
        evidence_id=f"{provider_id}:{status}:{_sha256([field, value, reason])[:16]}",
        payload=payload,
        endpoint="identity_resolution",
        direction="unknown",
        evidence_role="query_audit",
        provenance_status="audit_missing" if status == "not_queried" else "retrieved",
        retrieved_at=_now(),
        adapter_version=f"{provider_id}:query-contract-v1",
        source_version=f"{provider_id}:query-contract-v1",
        response_sha256=_sha256(payload),
        query_status=status,  # type: ignore[arg-type]
        evidence_type="query_audit",
        lookup_field=field,
        lookup_value=value,
        match_type=match_type,
        claim_ceiling="identity_or_query_audit_only_no_biological_conclusion",
    )


_AUTO_IDENTITY_CONFLICT_REASONS = {
    "cross_lookup_identity_conflict",
    "provider_compound_identity_conflict",
}


def _reconcile_provider_identity_conflicts(
    bundle: EvidenceBundle,
    resolution: IdentityResolution,
) -> None:
    """Gate a final bundle when one provider names multiple compounds.

    Local snapshot/QC rows are merged before gateway cache/live rows.  Checking
    only either side would miss identity drift between a frozen record and a
    refreshed provider response, so this reconciliation is safe to run after
    each merge and replaces its own earlier audit with the complete claim set.
    """

    claims_by_provider: dict[str, dict[str, list[dict[str, str]]]] = {}
    for hit in bundle.all_hits():
        identity_accession = _identity_accession(hit)
        if not identity_accession:
            continue
        provider = hit.provider_id or _provider_id(hit.adapter_id)
        claims_by_provider.setdefault(provider, {}).setdefault(
            identity_accession, []
        ).append(
            {
                "lookup_field": str(hit.lookup_field or ""),
                "lookup_value": str(hit.lookup_value or ""),
                "match_type": str(hit.match_type or ""),
                "evidence_id": str(hit.evidence_id or ""),
                "retrieved_at": str(hit.retrieved_at or ""),
                "source_version": str(hit.source_version or ""),
                "adapter_version": str(hit.adapter_version or ""),
            }
        )

    normalized_claims: dict[str, dict[str, list[dict[str, str]]]] = {}
    for provider in sorted(claims_by_provider):
        normalized_claims[provider] = {}
        for accession in sorted(claims_by_provider[provider]):
            normalized_claims[provider][accession] = sorted(
                claims_by_provider[provider][accession],
                key=lambda claim: (
                    claim["lookup_field"],
                    claim["lookup_value"],
                    claim["match_type"],
                    claim["evidence_id"],
                    claim["retrieved_at"],
                    claim["source_version"],
                    claim["adapter_version"],
                ),
            )

    # Re-running after live merge must replace, not duplicate, the local-only
    # conflict audit. Provider-supplied identity reviews use other reasons and
    # remain untouched.
    bundle.query_audit[:] = [
        hit
        for hit in bundle.query_audit
        if str((hit.payload or {}).get("reason") or "")
        not in _AUTO_IDENTITY_CONFLICT_REASONS
    ]

    identity_conflicts: list[dict[str, Any]] = []
    for provider, claims in normalized_claims.items():
        if len(claims) <= 1:
            continue
        lookup_pairs = {
            (claim["lookup_field"], claim["lookup_value"])
            for claim_rows in claims.values()
            for claim in claim_rows
        }
        reason = (
            "cross_lookup_identity_conflict"
            if len(lookup_pairs) > 1
            else "provider_compound_identity_conflict"
        )
        conflict = {
            "provider_id": provider,
            "reason": reason,
            "claims": claims,
        }
        identity_conflicts.append(conflict)
        review = _audit_hit(
            status="identity_review_required",
            resolution=resolution,
            provider_id=provider,
            reason=reason,
        )
        review.endpoint = (
            "cross_lookup_identity_resolution"
            if reason == "cross_lookup_identity_conflict"
            else "provider_compound_identity_resolution"
        )
        review.evidence_id = (
            f"{provider}:identity_review:"
            f"{_sha256([resolution.molecule_id, claims])[:16]}"
        )
        review.payload = conflict
        review.response_sha256 = _sha256(conflict)
        _add_hit(bundle, review)

    bundle.identity = {
        **dict(bundle.identity),
        "provider_identity_claims": normalized_claims,
        "identity_conflicts": identity_conflicts,
    }
    epa_conflicts = [
        conflict
        for conflict in identity_conflicts
        if conflict.get("provider_id") == "epa_ctx"
    ]
    if epa_conflicts:
        bundle.epa_audit = {
            **dict(bundle.epa_audit or {}),
            "query_status": "identity_review_required",
            "identity_conflicts": epa_conflicts,
            "ranking_effect": "none",
        }


def _record_mapping(record: Any) -> dict[str, Any]:
    if isinstance(record, Mapping):
        return dict(record)
    return {
        "molecule_id": getattr(record, "molecule_id", ""),
        "original_inchikey": getattr(record, "original_inchikey", ""),
        "standardized_inchikey": getattr(record, "standardized_inchikey", "")
        or getattr(record, "inchikey", ""),
        "cas": getattr(record, "cas", None),
        "standardized_smiles": getattr(record, "standardized_smiles", "")
        or getattr(record, "smiles", ""),
        "original_smiles": getattr(record, "original_smiles", ""),
        "standardization_steps": list(
            getattr(record, "standardization_steps", None) or []
        ),
    }


def _result_records(result: Any) -> list[Any]:
    if result is None:
        return []
    records = list(getattr(result, "molecule_records", None) or [])
    if records:
        return records
    records = list(getattr(result, "scored_molecules", None) or [])
    if records:
        return records
    return [
        *(getattr(result, "top_molecules", None) or []),
        *(getattr(result, "reserve_molecules", None) or []),
    ]


def _resolve_requested_identity(
    *,
    result: Any,
    molecule_index: Mapping[str, Any] | None,
    molecule_id: str | None,
    inchikey: str | None,
    cas: str | None,
    smiles: str | None,
) -> tuple[IdentityResolution, str]:
    requested_id = str(molecule_id or "").strip()
    entry: dict[str, Any] = {}
    if requested_id:
        current_records = _result_records(result)
        if current_records:
            matches = [
                _record_mapping(record)
                for record in current_records
                if str(getattr(record, "molecule_id", "") or _record_mapping(record).get("molecule_id") or "").strip()
                == requested_id
            ]
            if not matches:
                return resolve_identity(molecule_id=requested_id), "unknown_molecule_id"
        else:
            raw = (molecule_index or {}).get(requested_id)
            if raw is None:
                if not any((inchikey, cas, smiles)):
                    return resolve_identity(molecule_id=requested_id), "unknown_molecule_id"
                matches = []
            else:
                entries = raw if isinstance(raw, list) else [raw]
                matches = [
                    dict(item) for item in entries if isinstance(item, Mapping)
                ]
        if len(matches) > 1:
            base = resolution_from_mapping(matches[0])
            return (
                replace(
                    base,
                    status="identity_review_required",
                    conflicts=tuple([*base.conflicts, "duplicate_molecule_id"]),
                ),
                "identity_review_required",
            )
        if matches:
            entry = matches[0]

    record_key = str(
        entry.get("standardized_inchikey") or entry.get("inchikey") or ""
    ).strip()
    record_cas = str(entry.get("cas") or "").strip()
    conflicts: list[str] = []
    if inchikey and record_key and str(inchikey).strip().upper() != record_key.upper():
        conflicts.append("run_and_explicit_inchikey_conflict")
    if cas and record_cas and str(cas).strip() != record_cas:
        conflicts.append("run_and_explicit_cas_conflict")

    resolution = resolve_identity(
        molecule_id=requested_id or str(entry.get("molecule_id") or "direct_query"),
        original_inchikey=str(entry.get("original_inchikey") or ""),
        standardized_inchikey=str(inchikey or record_key or ""),
        cas=cas or entry.get("cas") or "",
        smiles=str(
            smiles
            or entry.get("standardized_smiles")
            or entry.get("smiles")
            or ""
        ),
        original_smiles=str(entry.get("original_smiles") or ""),
        standardization_steps=entry.get("standardization_steps") or (),
    )
    if conflicts:
        resolution = replace(
            resolution,
            status="identity_review_required",
            conflicts=tuple(dict.fromkeys([*resolution.conflicts, *conflicts])),
        )
    if resolution.requires_review:
        return resolution, "identity_review_required"
    if not resolution.is_resolved:
        return resolution, "audit_missing"
    return resolution, ""


def _merge_local_facade(
    facade: EvidenceFacade,
    resolution: IdentityResolution,
    provider_configs: Mapping[str, Mapping[str, Any]],
) -> tuple[EvidenceBundle, set[str]]:
    combined = EvidenceBundle(
        normalized_inchikey=resolution.identity.standardized_inchikey,
        input_structure_hash=_sha256(resolution.identity.to_dict()),
        queried_at=_now(),
        identity=resolution.to_dict(),
    )
    lookups: list[tuple[str, str, str]] = []
    for candidate in resolution.candidates:
        field = str(candidate.get("lookup_field") or "")
        value = str(candidate.get("lookup_value") or "")
        if field.endswith("inchikey") and value:
            lookups.append((field, value, str(candidate.get("match_type") or "")))
    if resolution.identity.cas:
        lookups.append(("cas", "", "cas_identifier"))
    if not lookups:
        lookups.append((resolution.lookup_field, "", resolution.match_type))

    seen_queries: set[tuple[str, str]] = set()
    local_ids: set[str] = set()
    lookup_audits: list[dict[str, Any]] = []
    for field, key, match_type in lookups:
        query_cas = resolution.identity.cas if field == "cas" else None
        signature = (key, query_cas or "")
        if signature in seen_queries:
            continue
        seen_queries.add(signature)
        # Preserve the candidate structure during CAS fallback so Facade can
        # compare the snapshot row's bound InChIKey and gate a cross-identity
        # CAS collision instead of accepting it as task evidence.
        facade_inchikey = key
        if field == "cas":
            facade_inchikey = (
                resolution.identity.standardized_inchikey
                or resolution.identity.original_inchikey
            )
        local = facade.query(
            inchikey=facade_inchikey,
            cas=query_cas,
            smiles=resolution.identity.standardized_smiles,
            allow_live=False,
        )
        lookup_audits.append(
            {
                "lookup_field": field or resolution.lookup_field,
                "lookup_value": key or query_cas or resolution.lookup_value,
                "match_type": match_type or resolution.match_type,
                "epa_audit": dict(local.epa_audit or {}),
                "dili_audit": dict(local.dili_audit or {}),
            }
        )
        for hit in local.all_hits():
            normalized = _normalize_hit(
                hit,
                resolution=resolution,
                provider_configs=provider_configs,
                lookup=(
                    field or resolution.lookup_field,
                    key or query_cas or resolution.lookup_value,
                    match_type or resolution.match_type,
                ),
            )
            _add_hit(combined, normalized)
            local_ids.add(normalized.evidence_id)
        combined.epa_audit = combined.epa_audit or dict(local.epa_audit or {})
        combined.dili_audit = combined.dili_audit or dict(local.dili_audit or {})

    combined.identity = {
        **dict(combined.identity),
        "lookup_audits": lookup_audits,
    }
    _reconcile_provider_identity_conflicts(combined, resolution)
    combined.annotate_evidence_types()
    combined.source_versions = combined.collect_source_versions()
    return combined, local_ids


def _epa_identity_adapter(
    task: Any,
    provider_config: Mapping[str, Any],
    *,
    risk_policy: Mapping[str, Any] | None = None,
    before_request: Callable[[], None] | None = None,
) -> list[EvidenceHit]:
    key = resolve_secret(
        "epa_ctx",
        explicit=str(provider_config.get("api_key") or "") or None,
        env_names=(
            str(provider_config.get("environment_fallback") or "CTX_API_KEY"),
            "CCTE_API_KEY",
            "MOLMIND_CTX_API_KEY",
        ),
    )
    if not key:
        raise PermissionError("EPA CTX credential is not configured")
    client = CtxClient(
        api_key=key,
        timeout=max(1, int(task.timeout_sec)),
        retries=1,
        base_url=str(
            provider_config.get("api_base") or "https://comptox.epa.gov/ctx-api"
        ),
        before_request=before_request,
    )
    hits = client.search_exact(str(task.lookup_value or ""))
    unique: dict[str, dict[str, Any]] = {}
    for row in hits:
        dtxsid = str(row.get("dtxsid") or row.get("DTXSID") or "").strip()
        if dtxsid:
            unique.setdefault(dtxsid, row)
    if not unique:
        return []
    retrieved_at = _now()
    source_url = (
        f"{str(provider_config.get('api_base') or 'https://comptox.epa.gov/ctx-api').rstrip('/')}"
        f"/chemical/search/equal/{quote(str(task.lookup_value or ''), safe='')}"
    )
    response_hash = _sha256(hits)
    if len(unique) > 1 or task.lookup_field == "cas":
        reason = "multiple_dtxsid_matches" if len(unique) > 1 else "cas_match_requires_structure_review"
        return [
            EvidenceHit(
                adapter_id="epa_ctx_tox_v1",
                provider_id="epa_ctx",
                query_type="query_audit",
                score=0.0,
                confidence=0.0,
                evidence_id=f"epa_ctx:identity_review:{_sha256(sorted(unique))[:16]}",
                payload={"reason": reason, "dtxsids": sorted(unique)},
                endpoint="candidate_bundle",
                direction="unknown",
                evidence_role="query_audit",
                provenance_status="retrieved",
                source_url=source_url,
                retrieved_at=retrieved_at,
                adapter_version="epa_ctx_tox_v1",
                source_version=str(provider_config.get("source_version") or "epa_ctx_live"),
                response_sha256=response_hash,
                query_status="identity_review_required",
                evidence_type="query_audit",
                accession=",".join(sorted(unique)),
                claim_ceiling="identity_review_only_no_efficacy_or_safety_claim",
            )
        ]
    dtxsid, row = next(iter(sorted(unique.items())))
    identity_hit = EvidenceHit(
            adapter_id="epa_ctx_identity_v1",
            provider_id="epa_ctx",
            query_type="annotation",
            score=0.0,
            confidence=0.0,
            evidence_id=f"epa_ctx:{dtxsid}:identity",
            payload={
                "dtxsid": dtxsid,
                "dtxcid": row.get("dtxcid"),
                "preferred_name": row.get("preferredName"),
                "casrn": row.get("casrn"),
            },
            endpoint="candidate_bundle",
            direction="unknown",
            evidence_role="annotation_only",
            provenance_status="retrieved",
            source_url=source_url,
            retrieved_at=retrieved_at,
            adapter_version="epa_ctx_tox_v1",
            source_version=str(provider_config.get("source_version") or "epa_ctx_live"),
            response_sha256=response_hash,
            query_status="annotation_only",
            evidence_type="identity_annotation",
            accession=dtxsid,
            claim_ceiling="chemical_identity_annotation_only_not_toxicity_clearance",
        )
    evidence = [identity_hit]
    candidate = query_candidate(client, {"dtxsid": dtxsid})
    responses = candidate.get("responses") or {}
    summaries = responses.get("bioactivity_summary") or []
    summary = summaries[0] if isinstance(summaries, list) and summaries else summaries
    if not isinstance(summary, Mapping):
        summary = {}
    policy = dict(risk_policy or {})
    screening_um = float(policy.get("cytotox_screening_um") or 10.0)
    tier = epa_cytotox_risk_tier(dict(summary), screening_um=screening_um)
    metrics = epa_cytotox_metrics(dict(summary))
    bio_url = f"{str(provider_config.get('api_base') or 'https://comptox.epa.gov/ctx-api').rstrip('/')}/bioactivity/data/summary/search/by-dtxsid/{dtxsid}"
    payload = {
        "dtxsid": dtxsid,
        "cytotox_summary": dict(summary),
        "cytotox_metrics": metrics,
        "risk_tier": tier,
        "screening_um": screening_um,
        "live_staged_policy": "exact_inchikey_only; strong_risk_only; no_safety_clearance",
    }
    if tier == "strong_risk":
        evidence.append(EvidenceHit(
            adapter_id="epa_ctx_tox_v1", provider_id="epa_ctx", query_type="tox",
            score=float(policy.get("max_risk_score") or 0.40),
            confidence=float(policy.get("risk_confidence") or 0.50),
            evidence_id=f"epa_ctx:{dtxsid}:cytotox_strong", payload=payload,
            endpoint="toxcast_cytotox_nhit", direction="risk", evidence_role="risk_signal",
            provenance_status="retrieved", source_url=bio_url, retrieved_at=retrieved_at,
            adapter_version="epa_ctx_tox_v1", source_version=str(provider_config.get("source_version") or "epa_ctx_live"),
            response_sha256=_sha256(summary), query_status="hit", evidence_type="endpoint_evidence",
            accession=dtxsid, claim_ceiling="candidate_risk_signal_only_not_safety_clearance",
        ))
    elif tier != "none":
        evidence.append(EvidenceHit(
            adapter_id="epa_ctx_tox_v1", provider_id="epa_ctx", query_type="annotation",
            score=0.0, confidence=0.0, evidence_id=f"epa_ctx:{dtxsid}:{tier}", payload=payload,
            endpoint="toxcast_cytotox_nhit", direction="unknown", evidence_role="annotation_only",
            provenance_status="retrieved", source_url=bio_url, retrieved_at=retrieved_at,
            adapter_version="epa_ctx_tox_v1", source_version=str(provider_config.get("source_version") or "epa_ctx_live"),
            response_sha256=_sha256(summary), query_status="annotation_only", evidence_type="toxicity_annotation",
            accession=dtxsid, claim_ceiling="toxicity_annotation_only_not_safety_or_efficacy_claim",
        ))
    for error in candidate.get("errors") or []:
        evidence.append(EvidenceHit(
            adapter_id="epa_ctx_tox_v1", provider_id="epa_ctx", query_type="query_audit",
            score=0.0, confidence=0.0, evidence_id=f"epa_ctx:{dtxsid}:query_failed:{error.get('endpoint')}",
            payload={"endpoint": error.get("endpoint"), "error_type": error.get("error_type")},
            endpoint=str(error.get("endpoint") or "candidate_bundle"), direction="unknown", evidence_role="query_audit",
            provenance_status="retrieved", source_url=bio_url, retrieved_at=retrieved_at,
            adapter_version="epa_ctx_tox_v1", source_version=str(provider_config.get("source_version") or "epa_ctx_live"),
            response_sha256=response_hash, query_status="query_failed", evidence_type="query_audit",
            accession=dtxsid, claim_ceiling="transport_failure_no_biological_conclusion",
        ))
    return evidence


def _default_adapters(
    facade: EvidenceFacade,
    provider_configs: Mapping[str, Mapping[str, Any]],
) -> dict[str, ProviderAdapter]:
    chembl_config = provider_configs.get("chembl") or {}
    pubchem_config = provider_configs.get("pubchem") or {}
    epa_config = provider_configs.get("epa_ctx") or {}
    request_gates = {
        "chembl": _ProviderRequestGate(chembl_config.get("rate_limit_per_sec")),
        "pubchem": _ProviderRequestGate(pubchem_config.get("rate_limit_per_sec")),
        "epa_ctx": _ProviderRequestGate(epa_config.get("rate_limit_per_sec")),
    }

    def chembl(task: Any) -> list[EvidenceHit]:
        if not str(task.lookup_field or "").endswith("inchikey"):
            return []
        with httpx.Client(timeout=task.timeout_sec, follow_redirects=True) as client:
            return facade._chembl_lipid(
                client,
                str(task.lookup_value or ""),
                api_base=str(
                    chembl_config.get("api_base")
                    or "https://www.ebi.ac.uk/chembl/api/data"
                ),
                before_request=request_gates["chembl"].wait,
            )

    def pubchem(task: Any) -> list[EvidenceHit]:
        if task.lookup_field not in {"original_inchikey", "standardized_inchikey", "cas"}:
            return []
        with httpx.Client(timeout=task.timeout_sec, follow_redirects=True) as client:
            return facade._pubchem_tox(
                client,
                str(task.lookup_value or ""),
                lookup_field=str(task.lookup_field or "inchikey"),
                lookup_value=str(task.lookup_value or ""),
                api_base=str(
                    pubchem_config.get("api_base")
                    or "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
                ),
                before_request=request_gates["pubchem"].wait,
            )

    def epa_ctx(task: Any) -> list[EvidenceHit]:
        return _epa_identity_adapter(
            task,
            epa_config,
            risk_policy=facade.cfg.evidence.get("epa_ctx") or {},
            before_request=request_gates["epa_ctx"].wait,
        )

    return {"chembl": chembl, "pubchem": pubchem, "epa_ctx": epa_ctx}


def _filter_hits(
    bundle: EvidenceBundle,
    *,
    providers: set[str] | None,
    query_types: set[str] | None,
) -> EvidenceBundle:
    if providers is None and query_types is None:
        return bundle
    filtered = EvidenceBundle(
        run_id=bundle.run_id,
        input_structure_hash=bundle.input_structure_hash,
        normalized_inchikey=bundle.normalized_inchikey,
        queried_at=bundle.queried_at,
        identity=dict(bundle.identity),
        epa_audit=dict(bundle.epa_audit),
        dili_audit=dict(bundle.dili_audit),
    )
    for hit in bundle.all_hits():
        provider_ok = providers is None or _provider_id(hit.adapter_id, hit.provider_id) in providers
        # Annotation/query audit reports what happened to a requested provider
        # even when the caller narrowed scientific endpoint types.
        type_ok = (
            query_types is None
            or hit.query_type in query_types
            or hit.evidence_role in {"annotation_only", "query_audit"}
        )
        if provider_ok and type_ok:
            _add_hit(filtered, hit)
    filtered.annotate_evidence_types()
    filtered.source_versions = filtered.collect_source_versions()
    return filtered


def _ranking_signature(hit: EvidenceHit) -> tuple[str, str, str, str]:
    return (
        str(hit.evidence_id or ""),
        str(hit.response_sha256 or ""),
        str(hit.source_version or ""),
        str(hit.adapter_version or ""),
    )


def _current_ranking_evidence_signatures(
    result: Any, molecule_id: str, molecule_index: Mapping[str, Any] | None = None
) -> set[tuple[str, str, str, str]]:
    """Return evidence provably consumed by the frozen score object.

    Attribution IDs alone are insufficient: pathway rows are attributed for
    explanation without entering the numeric formula, and a stable ID can be
    reused by a later provider version.  Require the original Run's normalized
    hit, endpoint role, response hash and version to match.
    """

    if not molecule_id:
        return set()
    if result is None and molecule_index is not None:
        raw = molecule_index.get(molecule_id)
        entries = raw if isinstance(raw, list) else [raw]
        for entry in entries:
            if isinstance(entry, Mapping):
                return {
                    tuple(str(item) for item in signature)
                    for signature in entry.get("ranking_evidence_signatures", [])
                    if isinstance(signature, (list, tuple)) and len(signature) == 4
                }
        return set()
    if result is None:
        return set()
    records = list(getattr(result, "scored_molecules", None) or [])
    if not records:
        records = [
            *(getattr(result, "top_molecules", None) or []),
            *(getattr(result, "reserve_molecules", None) or []),
        ]
    for record in records:
        mapping = _record_mapping(record)
        if str(mapping.get("molecule_id") or "") != molecule_id:
            continue
        return {
            _ranking_signature(hit)
            for hit in (getattr(record, "evidence_hits", None) or [])
            if isinstance(hit, EvidenceHit)
            and hit.evidence_role in {"task_evidence", "risk_signal"}
            and hit.query_type in {"lipid", "tox"}
            and hit.query_status in {"hit", "exact_hit", "analogue_hit"}
            and bool(hit.response_sha256)
            and bool(hit.source_version or hit.adapter_version)
        }
    return set()


def _evidence_item(
    hit: EvidenceHit,
    ranking_signatures: set[tuple[str, str, str, str]],
) -> dict[str, Any]:
    participates = _ranking_signature(hit) in ranking_signatures
    item = {
        "evidence_id": hit.evidence_id,
        "provider": hit.provider_id or _provider_id(hit.adapter_id),
        "adapter_id": hit.adapter_id,
        "query_type": hit.query_type,
        "evidence_role": hit.evidence_role,
        "evidence_type": hit.evidence_type,
        "query_status": _canonical_status(hit.query_status, hit),
        "lookup_field": hit.lookup_field,
        "lookup_value": hit.lookup_value,
        "match_type": hit.match_type,
        "endpoint": hit.endpoint,
        "direction": hit.direction,
        "score": float(hit.score or 0.0),
        "confidence": float(hit.confidence or 0.0),
        "source_url": hit.source_url,
        "accession": hit.accession,
        "retrieved_at": hit.retrieved_at,
        "source_version": hit.source_version,
        "adapter_version": hit.adapter_version,
        "response_sha256": hit.response_sha256,
        "claim_ceiling": hit.claim_ceiling,
        "participates_in_ranking": participates,
        "ranking_relation": (
            "verified_frozen_scoring_input" if participates else "annotation_or_unverified_for_current_run"
        ),
    }
    if hit.evidence_role == "query_audit":
        payload = hit.payload or {}
        item["audit_detail"] = {
            key: payload[key]
            for key in (
                "reason",
                "action",
                "conflicts",
                "claims",
                "cids",
                "dtxsids",
                "cached_status",
                "cached_retrieved_at",
                "cache_expires_at",
                "next_retry_at",
                "verified_empty_is_not_biological_negative",
                "response_sha256_basis",
            )
            if key in payload
        }
    return item


def _provider_statuses(items: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(str(item.get("provider") or "local"), []).append(item)
    result: dict[str, dict[str, Any]] = {}
    for provider in sorted(grouped):
        rows = grouped[provider]
        status = max(
            (str(row.get("query_status") or "not_queried") for row in rows),
            key=lambda value: _STATUS_PRIORITY.get(value, -1),
        )
        result[provider] = {
            "status": status,
            "query_status": status,
            "hit_count": sum(
                row.get("evidence_role") not in {"query_audit"}
                and row.get("query_status") in {"hit", "annotation_only"}
                for row in rows
            ),
            "evidence_ids": [str(row["evidence_id"]) for row in rows],
            "statuses": sorted(
                {str(row.get("query_status") or "not_queried") for row in rows},
                key=lambda value: (-_STATUS_PRIORITY.get(value, -1), value),
            ),
            "status_counts": {
                status_name: sum(
                    str(row.get("query_status") or "not_queried") == status_name
                    for row in rows
                )
                for status_name in sorted(
                    {str(row.get("query_status") or "not_queried") for row in rows}
                )
            },
            "lookup_field": next((row["lookup_field"] for row in rows if row.get("lookup_field")), ""),
            "lookup_value": next((row["lookup_value"] for row in rows if row.get("lookup_value")), ""),
            "match_type": next((row["match_type"] for row in rows if row.get("match_type")), ""),
        }
    return result


def _identity_conflict_details(
    bundle: EvidenceBundle,
    resolution: IdentityResolution,
) -> tuple[list[str], list[dict[str, Any]]]:
    reasons = list(resolution.conflicts)
    details: list[dict[str, Any]] = []
    for hit in bundle.query_audit:
        if _canonical_status(hit.query_status, hit) != "identity_review_required":
            continue
        payload = hit.payload or {}
        reason = str(payload.get("reason") or "provider_identity_review_required")
        if reason not in reasons:
            reasons.append(reason)
        details.append(
            {
                "provider": hit.provider_id or _provider_id(hit.adapter_id),
                "evidence_id": hit.evidence_id,
                "reason": reason,
                "claims": payload.get("claims") or {},
                "cids": payload.get("cids") or [],
                "dtxsids": payload.get("dtxsids") or [],
            }
        )
    return reasons, details


def _card_claim_ceiling(
    overall_status: str,
    items: Sequence[dict[str, Any]],
) -> str:
    if overall_status == "identity_review_required":
        return "identity_review_only_no_efficacy_or_safety_claim"
    if overall_status in {
        "verified_empty",
        "query_failed",
        "auth_missing",
        "not_queried",
    }:
        return "query_transport_audit_only_no_biological_conclusion"
    scientific = [
        item
        for item in items
        if item.get("query_status") == "hit"
        and item.get("evidence_role") not in {"query_audit", "annotation_only"}
    ]
    if any(
        item.get("evidence_role") == "task_evidence"
        and item.get("query_type") == "lipid"
        and item.get("direction") not in {"risk", "negative", "contradicts"}
        for item in scientific
    ):
        return "candidate_preclinical_evidence_only_not_clinical_efficacy_or_safety"
    if any(
        item.get("query_type") == "tox"
        and item.get("direction") in {"risk", "negative", "contradicts", "adverse"}
        for item in scientific
    ):
        return "candidate_risk_signal_only_not_safety_clearance"
    if scientific:
        return "mechanism_context_only_not_candidate_efficacy"
    return "database_annotation_only_not_efficacy_or_safety_evidence"


def _is_terminal_local_provider_hit(
    provider: str,
    hit: EvidenceHit,
    *,
    allow_live: bool,
    force_refresh: bool,
    ttl_days_by_status: Mapping[str, float],
) -> bool:
    """Return whether local data represents the provider's primary lookup.

    Public assay-grain rows can mention ChEMBL/PubChem while representing a
    different endpoint.  They are useful local evidence but must not suppress
    the provider's configured compound/activity lookup.  Frozen primary
    adapter rows and provider query audits do suppress that duplicate request.
    """

    adapter = str(hit.adapter_id or "").lower()
    primary_markers = {
        "chembl": ("chembl_lipid_v",),
        "pubchem": ("pubchem_tox_v",),
        "epa_ctx": ("epa_ctx_candidate", "epa_ctx_identity", "epa_ctx_tox"),
        "bindingdb": ("bindingdb",),
    }
    if not any(marker in adapter for marker in primary_markers.get(provider, ())):
        return False
    status = _canonical_status(hit.query_status, hit)
    if status in {"hit", "annotation_only", "verified_empty"}:
        if not allow_live:
            return True
        if force_refresh:
            return False
        if str((hit.payload or {}).get("retrieved_at_basis") or "") == (
            "local_query_time_source_timestamp_missing"
        ):
            return False
        retrieved_at = _parse_timestamp(hit.retrieved_at)
        if retrieved_at is None:
            return False
        ttl_days = max(0.0, float(ttl_days_by_status.get(status, 0.0)))
        return retrieved_at + timedelta(days=ttl_days) > datetime.now(timezone.utc)
    return status == "identity_review_required"


@dataclass
class EvidenceQueryResponse:
    ok: bool
    error_code: str
    message: str
    card: dict[str, Any]
    bundle: EvidenceBundle
    degraded_channels: list[str]
    identity: dict[str, Any]


def _identity_view(resolution: IdentityResolution) -> dict[str, Any]:
    return {
        **resolution.identity.to_dict(),
        "status": resolution.status,
        "lookup_field": resolution.lookup_field,
        "lookup_value": resolution.lookup_value,
        "match_type": resolution.match_type,
        "conflicts": list(resolution.conflicts),
        "candidates": [dict(item) for item in resolution.candidates],
    }


def _early_response(
    resolution: IdentityResolution,
    *,
    error_code: str,
    message: str,
    event_sink: EventSink | None,
) -> EvidenceQueryResponse:
    status = "identity_review_required" if resolution.requires_review else "not_queried"
    bundle = EvidenceBundle(
        normalized_inchikey=resolution.identity.standardized_inchikey,
        input_structure_hash=_sha256(resolution.identity.to_dict()),
        queried_at=_now(),
        identity=resolution.to_dict(),
    )
    hit = _audit_hit(
        status=status,
        resolution=resolution,
        reason=error_code,
    )
    _add_hit(bundle, hit)
    identity = _identity_view(resolution)
    card_status = "identity_review_required" if resolution.requires_review else "audit_missing"
    card = {
        "title": "候选分子证据卡",
        "status": card_status,
        "identity": identity,
        "summary": message,
        "provider_statuses": {"identity_resolver": {"status": status}},
        "evidence_items": [_evidence_item(hit, set())],
        "identity_review_required": "; ".join(resolution.conflicts),
        "scientific_conclusion": (
            "身份存在冲突；任何关联证据均不得提高效力或安全置信度。"
            if resolution.requires_review
            else "缺少可解析身份；未执行来源查询，不能作出生物学结论。"
        ),
        "claim_ceiling": "identity_or_query_audit_only",
        "recommendation": "请补充当前 Run 的 molecule_id，或提供可解析的 InChIKey、CAS、SMILES。",
        "degraded_channels": [],
        "writes_selection": False,
    }
    _emit(
        event_sink,
        {
            "type": "identity_conflict" if resolution.requires_review else "degraded",
            "status": card_status,
            "molecule_id": resolution.molecule_id,
            "identity": identity,
            "message": message,
        },
    )
    _emit(
        event_sink,
        {
            "type": "query_summary",
            "status": card_status,
            "molecule_id": resolution.molecule_id,
            "identity": identity,
            "hit_count": 0,
            "message": message,
        },
    )
    return EvidenceQueryResponse(
        ok=False,
        error_code=error_code,
        message=message,
        card=card,
        bundle=bundle,
        degraded_channels=[],
        identity=identity,
    )


def run_query_evidence_impl(
    *,
    result: Any = None,
    molecule_index: Mapping[str, Any] | None = None,
    molecule_id: str | None = None,
    inchikey: str | None = None,
    cas: str | None = None,
    smiles: str | None = None,
    providers: Sequence[str] | None = None,
    query_types: Sequence[str] | None = None,
    allow_live: bool = False,
    force_refresh: bool = False,
    event_sink: EventSink | None = None,
    snapshot_dir: Path | None = None,
    provider_config_path: Path | None = None,
    cache_path: Path | None = None,
    provider_adapters: Mapping[str, ProviderAdapter] | None = None,
    total_timeout_sec: float | None = None,
    deadline: float | datetime | None = None,
    cancel_event: threading.Event | None = None,
) -> EvidenceQueryResponse:
    """Execute one auditable evidence lookup without mutating ``result``."""

    started_monotonic = time.monotonic()
    if total_timeout_sec is not None:
        total_deadline = started_monotonic + max(0.0, float(total_timeout_sec))
    elif isinstance(deadline, datetime):
        target = deadline if deadline.tzinfo else deadline.replace(tzinfo=timezone.utc)
        total_deadline = started_monotonic + max(
            0.0, (target - datetime.now(timezone.utc)).total_seconds()
        )
    elif deadline is not None:
        total_deadline = float(deadline)
    else:
        total_deadline = None
    cancellation = cancel_event or threading.Event()

    resolution, identity_error = _resolve_requested_identity(
        result=result,
        molecule_index=molecule_index,
        molecule_id=molecule_id,
        inchikey=inchikey,
        cas=cas,
        smiles=smiles,
    )
    if identity_error:
        messages = {
            "unknown_molecule_id": f"当前 Run 中不存在 molecule_id={molecule_id!r}；未猜测其他候选。",
            "identity_review_required": "分子身份存在冲突，需要人工核对后才能查询或提升证据置信度。",
            "audit_missing": "缺少可解析的 molecule_id / InChIKey / CAS / SMILES；未执行查询。",
        }
        return _early_response(
            resolution,
            error_code=identity_error,
            message=messages[identity_error],
            event_sink=event_sink,
        )

    raw_config = load_provider_config(provider_config_path)
    cache_config = raw_config.get("cache") if isinstance(raw_config, Mapping) else None
    provider_configs_raw = raw_config.get("providers", raw_config)
    provider_configs: dict[str, Mapping[str, Any]] = {
        str(key): value
        for key, value in provider_configs_raw.items()
        if isinstance(value, Mapping)
    }
    requested_providers = None
    unknown_providers: list[str] = []
    if providers is not None:
        requested_providers = []
        for value in providers:
            provider = _PROVIDER_ALIASES.get(str(value).lower(), str(value).lower())
            if provider not in provider_configs:
                unknown_providers.append(provider)
            elif provider not in requested_providers:
                requested_providers.append(provider)
    else:
        requested_providers = [
            provider
            for provider, config in provider_configs.items()
            if config.get("enabled", True)
            and config.get("query_tool_default", bool(config.get("identity_order")))
        ]
    requested_types = (
        list(dict.fromkeys(str(value) for value in query_types if str(value)))
        if query_types is not None
        else None
    )

    identity = _identity_view(resolution)
    _emit(
        event_sink,
        {
            "type": "query_plan",
            "molecule_id": resolution.molecule_id,
            "identity": identity,
            "providers": requested_providers,
            "query_types": requested_types or [],
            "allow_live": bool(allow_live),
            "force_refresh": bool(force_refresh),
            "deadline": (
                round(max(0.0, total_deadline - time.monotonic()), 3)
                if total_deadline is not None
                else None
            ),
            "local_sources": [
                "frozen_snapshot",
                "local_public_qc",
                "dilirank_gate",
                "epa_ctx_frozen_stage",
            ],
            "cached_remote_sources": [],
            "remote_provider_plan": requested_providers if allow_live else [],
            "skipped_or_unsupported_sources": (
                [] if allow_live else list(requested_providers)
            ),
            "message": "snapshot -> local QC -> query-state cache -> optional live -> evidence bundle",
        },
    )

    cfg = load_config(mode="auto", use_snapshot=True, allow_live=False)
    facade = EvidenceFacade(cfg, snapshot_dir=Path(snapshot_dir or SNAPSHOT_DIR))
    local_bundle, local_ids = _merge_local_facade(facade, resolution, provider_configs)
    # An omitted provider filter still shows every local/QC source.  The
    # configured default list only bounds which missing remote sources may be
    # consulted; it must not hide local DILI/QC annotations from the card.
    selected_provider_set = set(requested_providers) if providers is not None else None
    requested_type_set = set(requested_types) if requested_types is not None else None
    bundle = _filter_hits(
        local_bundle,
        providers=selected_provider_set,
        query_types=requested_type_set,
    )
    local_provider_ids = {
        _provider_id(hit.adapter_id, hit.provider_id)
        for hit in bundle.all_hits()
        if hit.evidence_id in local_ids
        and hit.query_status
        in {"hit", "annotation_only", "verified_empty", "identity_review_required"}
    }
    ttl_config = ((cache_config or {}).get("ttl_days") or {})
    ttl_days_by_status: dict[str, float] = {}
    for status_name, default_ttl in (
        ("hit", 90.0),
        ("annotation_only", 30.0),
        ("verified_empty", 14.0),
    ):
        try:
            ttl_days_by_status[status_name] = float(
                ttl_config.get(status_name, default_ttl)
            )
        except (TypeError, ValueError):
            ttl_days_by_status[status_name] = default_ttl
    terminal_local_providers = {
        provider
        for provider in local_provider_ids
        if any(
            hit.evidence_id in local_ids
            and _provider_id(hit.adapter_id, hit.provider_id) == provider
            and _is_terminal_local_provider_hit(
                provider,
                hit,
                allow_live=bool(allow_live),
                force_refresh=bool(force_refresh),
                ttl_days_by_status=ttl_days_by_status,
            )
            for hit in bundle.all_hits()
        )
    }
    for provider in sorted(local_provider_ids):
        provider_hits = [
            hit
            for hit in bundle.all_hits()
            if _provider_id(hit.adapter_id, hit.provider_id) == provider
        ]
        _emit(
            event_sink,
            {
                "type": "local_hit",
                "provider": provider,
                "status": max(
                    (_canonical_status(hit.query_status, hit) for hit in provider_hits),
                    key=lambda value: _STATUS_PRIORITY.get(value, -1),
                    default="not_queried",
                ),
                "hit_count": len(provider_hits),
                "evidence_ids": [hit.evidence_id for hit in provider_hits],
                "lookup_field": resolution.lookup_field,
                "lookup_value": resolution.lookup_value,
                "match_type": resolution.match_type,
            },
        )

    _emit(
        event_sink,
        {
            "type": "query_plan",
            "phase": "resolved",
            "molecule_id": resolution.molecule_id,
            "local_sources": sorted(local_provider_ids),
            "cached_remote_sources": sorted(terminal_local_providers),
            "remote_provider_plan": (
                sorted(
                    provider
                    for provider in requested_providers
                    if provider not in terminal_local_providers
                )
                if allow_live
                else []
            ),
            "skipped_or_unsupported_sources": (
                sorted(
                    provider
                    for provider in requested_providers
                    if provider not in terminal_local_providers
                )
                if not allow_live
                else sorted(unknown_providers)
            ),
            "allow_live": bool(allow_live),
            "force_refresh": bool(force_refresh),
            "deadline": (
                round(max(0.0, total_deadline - time.monotonic()), 3)
                if total_deadline is not None
                else None
            ),
        },
    )

    configured_cache = str((cache_config or {}).get("state_db") or "data/public/cache/evidence_query_state.sqlite")
    state_path = Path(cache_path) if cache_path is not None else REPO_ROOT / configured_cache
    cache = EvidenceQueryCache(state_path, config=raw_config)
    retrieval = None
    try:
        defaults = _default_adapters(facade, provider_configs)
        adapters = {**defaults, **dict(provider_adapters or {})}
        missing_providers = [
            provider
            for provider in requested_providers
            if provider not in terminal_local_providers
        ]
        retriever = EvidenceRetriever(cache, provider_configs, adapters)

        def gateway_event(event: dict[str, Any]) -> None:
            # The retriever's aggregate is useful in its own result, but the
            # Tool emits the final candidate-level query_summary after local
            # and gateway evidence have been merged.  Avoid two competing
            # "final" summaries in the Agent stream.
            if str(event.get("type") or "") in {"query_summary", "query_plan"}:
                return
            _emit(event_sink, event)

        retrieval = retriever.query(
            [resolution],
            providers=missing_providers,
            query_types=requested_types,
            allow_live=bool(allow_live),
            force_refresh=bool(force_refresh and allow_live),
            event_sink=gateway_event,
            deadline=total_deadline,
            cancel_event=cancellation,
        )
    finally:
        cache.close()

    molecule_key = resolution.molecule_id or resolution.lookup_value
    if retrieval is not None:
        remote_hits = [
            *(retrieval.hits_by_molecule.get(molecule_key, []) or []),
            *(retrieval.audits_by_molecule.get(molecule_key, []) or []),
        ]
        for hit in remote_hits:
            normalized = _normalize_hit(
                hit,
                resolution=resolution,
                provider_configs=provider_configs,
                provider_hint=hit.provider_id,
            )
            _add_hit(bundle, normalized)
        bundle.query_plan = [
            {
                "provider_id": task.provider_id,
                "endpoint": task.endpoint,
                "query_type": task.query_type,
                "action": task.action,
                "lookup_field": task.lookup_field,
                "lookup_value": task.lookup_value,
                "match_type": task.match_type,
            }
            for task in retrieval.tasks
        ]
        bundle.degraded_channels = list(retrieval.degraded_channels)

    for provider in unknown_providers:
        audit = _normalize_hit(
            _audit_hit(
                status="not_queried",
                resolution=resolution,
                provider_id=provider,
                reason="unknown_provider",
            ),
            resolution=resolution,
            provider_configs=provider_configs,
            provider_hint=provider,
        )
        _add_hit(bundle, audit)

    # Cache/live evidence is intentionally merged only after local evidence.
    # Reconcile compound accessions now so provider identity drift across those
    # two layers becomes a non-scoring identity review instead of two hits.
    _reconcile_provider_identity_conflicts(bundle, resolution)

    for channel in cfg.degraded_channels:
        if channel not in bundle.degraded_channels:
            bundle.degraded_channels.append(channel)
    bundle.degraded_channels = list(dict.fromkeys(bundle.degraded_channels))
    for channel_name in ("lipid", "tox", "novelty", "pathway", "annotation", "query_audit"):
        channel = getattr(bundle, channel_name)
        channel.sort(
            key=lambda hit: (
                hit.provider_id or _provider_id(hit.adapter_id),
                hit.query_type,
                hit.endpoint,
                hit.evidence_id,
            )
        )
    bundle.annotate_evidence_types()
    bundle.source_versions = bundle.collect_source_versions()

    ranking_signatures = _current_ranking_evidence_signatures(
        result, resolution.molecule_id, molecule_index
    )
    items = [
        _evidence_item(hit, ranking_signatures) for hit in bundle.all_hits()
    ]
    provider_statuses = _provider_statuses(items)
    statuses = [row["status"] for row in provider_statuses.values()]
    overall_status = max(
        statuses,
        key=lambda value: _STATUS_PRIORITY.get(value, -1),
        default="not_queried",
    )
    if bundle.has_identity_review_required:
        overall_status = "identity_review_required"
    degraded = list(bundle.degraded_channels)
    for provider, row in provider_statuses.items():
        if row["status"] in {"query_failed", "auth_missing"}:
            channel = f"{provider}:{row['status']}"
            if channel not in degraded:
                degraded.append(channel)

    participated = sum(bool(item["participates_in_ranking"]) for item in items)
    task_only = sum(
        item["evidence_role"] == "task_evidence"
        and not item["participates_in_ranking"]
        for item in items
    )
    identity_conflict_reasons, identity_conflict_details = _identity_conflict_details(
        bundle, resolution
    )
    if overall_status == "identity_review_required":
        conclusion = "身份匹配存在歧义；这些证据不得提高效力或安全置信度，需人工复核。"
    elif overall_status == "hit":
        conclusion = (
            f"发现候选级证据；其中 {participated} 条可确认参与当前排名，"
            f"{task_only} 条任务证据未参与当前排名。不得外推为临床有效或安全。"
        )
    elif overall_status == "annotation_only":
        conclusion = "仅发现身份/机制注释，不能据此声称降脂有效或低毒。"
    elif overall_status == "verified_empty":
        conclusion = "来源返回未发现记录；这不是生物学阴性、无效或无毒结论。"
    else:
        conclusion = "查询未产生可解释为效力或安全的证据；失败、缺凭据和未查询均不是阴性。"

    if not allow_live and any(status == "not_queried" for status in statuses):
        recommendation = "如需补查远端来源，请显式设置 allow_live=true；默认离线结果保持可复现。"
    elif any(status in {"query_failed", "auth_missing"} for status in statuses):
        recommendation = "检查相应 provider 的凭据/退避窗口后再显式联网重试；其他来源结果仍有效。"
    else:
        recommendation = "若要让新证据影响未来排名，须先规范化、审计并冻结为 snapshot，再离线复跑。"

    message = (
        f"证据查询完成：候选 {resolution.molecule_id or resolution.lookup_value}，"
        f"状态 {overall_status}，共 {len(items)} 条；主榜未修改。"
    )
    card = {
        "title": "候选分子证据卡",
        "status": overall_status,
        "molecule_id": resolution.molecule_id,
        "identity": identity,
        "summary": message,
        "provider_statuses": provider_statuses,
        "evidence_items": items,
        "ranking_evidence_count": participated,
        "annotation_or_unranked_count": len(items) - participated,
        "identity_review_required": (
            "; ".join(identity_conflict_reasons)
            if bundle.has_identity_review_required
            else ""
        ),
        "identity_conflicts": identity_conflict_details,
        "scientific_conclusion": conclusion,
        "claim_ceiling": _card_claim_ceiling(overall_status, items),
        "recommendation": recommendation,
        "allow_live": bool(allow_live),
        "force_refresh": bool(force_refresh),
        "degraded_channels": degraded,
        "writes_selection": False,
    }
    _emit(
        event_sink,
        {
            "type": "query_summary",
            "status": overall_status,
            "molecule_id": resolution.molecule_id,
            "identity": identity,
            "hit_count": len(items),
            "degraded_channels": degraded,
            "allow_live": bool(allow_live),
            "message": message,
        },
    )
    ok = overall_status not in {
        "query_failed",
        "auth_missing",
        "identity_review_required",
        "not_queried",
    }
    return EvidenceQueryResponse(
        ok=ok,
        error_code="" if ok else overall_status,
        message=message,
        card=card,
        bundle=bundle,
        degraded_channels=degraded,
        identity=identity,
    )


__all__ = ["EvidenceQueryResponse", "run_query_evidence_impl"]
