"""Deterministic planning for local-first evidence enrichment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from plugins.molmind_core.scientific.evidence_gateway.cache import (
    EvidenceQueryCache,
    QueryDecision,
)
from plugins.molmind_core.scientific.evidence_gateway.contract import (
    CLASSIFICATION_RULES_VERSION,
    IDENTITY_RESOLVER_VERSION,
    NORMALIZED_EVIDENCE_SCHEMA_VERSION,
    content_sha256,
)
from plugins.molmind_core.scientific.evidence_gateway.identity import (
    IdentityResolution,
    MoleculeIdentity,
    resolution_from_mapping,
)


@dataclass(frozen=True)
class EvidenceQueryTask:
    """One provider lookup, including the non-secret execution policy."""

    # Keep the historical positional fields first for compatibility.
    provider_id: str
    molecule_id: str
    entity_key: str
    endpoint: str
    lookup_field: str | None
    lookup_value: str | None
    decision: QueryDecision
    query_type: str = "identity"
    match_type: str = ""
    identity_status: str = "hit"
    endpoint_url: str = ""
    adapter_version: str = ""
    query_contract_hash: str = ""
    concurrency: int = 1
    timeout_sec: float = 20.0
    rate_limit_per_sec: float = 0.0
    retry_attempts: int = 0
    retry_backoff_sec: float = 0.0
    circuit_fail_threshold: int = 3
    circuit_reset_sec: float = 60.0

    @property
    def action(self) -> str:
        return self.decision.action

    @property
    def cache_key(self) -> tuple[str, str, str, str]:
        return (
            self.provider_id,
            self.entity_key,
            self.endpoint,
            self.query_contract_hash,
        )

    @property
    def request_key(self) -> tuple[str, str, str, str, str, str]:
        """Key shared by molecule aliases that would make the same request."""

        return (
            self.provider_id,
            self.endpoint,
            self.query_type,
            self.lookup_field or "",
            self.lookup_value or "",
            self.query_contract_hash,
        )


def _as_resolution(entity: Mapping[str, Any] | MoleculeIdentity | IdentityResolution) -> IdentityResolution:
    if isinstance(entity, IdentityResolution):
        return entity
    if isinstance(entity, MoleculeIdentity):
        return resolution_from_mapping(entity.to_dict())
    if isinstance(entity.get("identity"), Mapping):
        # Accept the JSON-safe IdentityResolution representation used by tools.
        nested = dict(entity["identity"])
        nested.setdefault("molecule_id", entity.get("molecule_id") or entity.get("id"))
        resolution = resolution_from_mapping(nested)
        if str(entity.get("status") or "") == "identity_review_required":
            # Re-resolving the normalized values will normally reproduce the
            # conflict.  Preserve the gate if an upstream resolver supplied it.
            return IdentityResolution(
                identity=resolution.identity,
                status="identity_review_required",
                candidates=resolution.candidates,
                lookup_field=resolution.lookup_field,
                lookup_value=resolution.lookup_value,
                match_type=resolution.match_type,
                conflicts=tuple(str(item) for item in entity.get("conflicts") or ()),
                notes=resolution.notes,
            )
        return resolution
    return resolution_from_mapping(entity)


def _selected_provider_ids(
    provider_configs: Mapping[str, Mapping[str, Any]],
    requested: Sequence[str] | None,
) -> list[str]:
    if requested is None:
        return [str(provider_id) for provider_id in provider_configs]
    return list(dict.fromkeys(str(item) for item in requested if str(item) in provider_configs))


def _query_specs(
    config: Mapping[str, Any],
    requested: Sequence[str] | None,
    default_endpoint: str,
) -> list[tuple[str, str, Mapping[str, Any], bool]]:
    configured = config.get("query_types")
    specs: list[tuple[str, Mapping[str, Any]]] = []
    has_explicit_specs = bool(configured)
    if isinstance(configured, Mapping):
        for query_type, raw in configured.items():
            specs.append(
                (
                    str(query_type),
                    raw if isinstance(raw, Mapping) else {},
                )
            )
    elif isinstance(configured, (list, tuple)):
        specs.extend((str(query_type), {}) for query_type in configured)

    if requested is not None:
        by_name = {name: details for name, details in specs}
        specs = [
            (
                str(name),
                (
                    by_name.get(str(name), {"unsupported_query_type": True})
                    if by_name
                    else {}
                ),
            )
            for name in dict.fromkeys(requested)
        ]
        has_explicit_specs = True
    elif not specs:
        specs = [(str(config.get("query_type") or "identity"), {})]

    result: list[tuple[str, str, Mapping[str, Any], bool]] = []
    endpoint_indexes: dict[tuple[str, bool], int] = {}
    endpoints = config.get("endpoints") if isinstance(config.get("endpoints"), Mapping) else {}
    for query_type, details in specs:
        supported = not bool(details.get("unsupported_query_type"))
        endpoint_value = details.get("endpoint") or endpoints.get(query_type) or config.get("endpoint")
        if not endpoint_value:
            endpoint_value = query_type if has_explicit_specs else default_endpoint
        endpoint_name = (
            str(endpoint_value) if supported else "unsupported_query_type"
        )
        # A provider adapter may return multiple scientific evidence channels
        # from one endpoint (for example one ChEMBL activity response).  Keep a
        # single transport task and carry the requested channels as an ordered,
        # comma-separated filter for the retriever.
        group_key = (endpoint_name, supported)
        if group_key in endpoint_indexes:
            index = endpoint_indexes[group_key]
            existing_types, _, existing_details, _ = result[index]
            type_names = list(dict.fromkeys(existing_types.split(",") + [query_type]))
            result[index] = (
                ",".join(type_names),
                endpoint_name,
                existing_details,
                supported,
            )
        else:
            endpoint_indexes[group_key] = len(result)
            result.append((query_type, endpoint_name, details, supported))
    return result


def _number(value: Any, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(value))
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def query_contract_hash(
    provider_id: str,
    config: Mapping[str, Any],
    *,
    endpoint: str,
    query_type: str,
    query_config: Mapping[str, Any] | None = None,
) -> str:
    """Hash every provider setting that can change normalized query content."""

    # Minimal third-party/test adapters predating the contract field retain
    # the historical bundle key. Production providers carry an adapter or
    # transport version and therefore always get a non-empty contract hash.
    if not config.get("adapter_version") and not config.get("transport_api_version"):
        return ""

    excluded = {
        "enabled",
        "query_tool_default",
        "live_supported",
        "concurrency",
        "timeout_sec",
        "rate_limit_per_sec",
        "retry_attempts",
        "retry_backoff_sec",
        "circuit_fail_threshold",
        "circuit_reset_sec",
        "credential",
        "credential_storage",
        "environment_fallback",
        "environment_file_fallback",
        "api_key_obfuscated",
    }
    content_config = {
        str(key): value for key, value in config.items() if str(key) not in excluded
    }
    return content_sha256(
        {
            "provider_id": provider_id,
            "adapter_version": str(config.get("adapter_version") or ""),
            "normalized_evidence_schema_version": NORMALIZED_EVIDENCE_SCHEMA_VERSION,
            "identity_resolver_version": IDENTITY_RESOLVER_VERSION,
            "classification_rules_version": CLASSIFICATION_RULES_VERSION,
            "transport_api_version": str(config.get("transport_api_version") or ""),
            "endpoint": endpoint,
            "query_type": query_type,
            "identity_order": list(config.get("identity_order") or ()),
            "query_config": dict(query_config or {}),
            "content_config": content_config,
        }
    )


def plan_provider_queries(
    cache: EvidenceQueryCache,
    entities: Iterable[Mapping[str, Any] | MoleculeIdentity | IdentityResolution],
    provider_configs: Mapping[str, Mapping[str, Any]],
    *,
    online: bool,
    endpoint: str = "identity_lookup",
    providers: Sequence[str] | None = None,
    query_types: Sequence[str] | None = None,
    force_refresh: bool = False,
) -> list[EvidenceQueryTask]:
    """Plan deterministic local/remote work without issuing network requests.

    Identity is resolved once and then intersected with each provider's declared
    ``identity_order``.  The cache entity key is the value actually looked up,
    rather than an unrelated globally preferred identifier.
    """

    tasks: list[EvidenceQueryTask] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    provider_ids = _selected_provider_ids(provider_configs, providers)

    for entity_index, entity in enumerate(entities):
        resolution = _as_resolution(entity)
        molecule_id = (
            resolution.molecule_id
            or resolution.lookup_value
            or f"entity:{entity_index}"
        )
        identity_payload = resolution.identity.to_dict()

        for provider_id in provider_ids:
            config = provider_configs[provider_id]
            identity_order = config.get("identity_order") or ()
            field, value, match_type = resolution.lookup_for(identity_order)
            entity_key = value or resolution.lookup_value or molecule_id
            if entity_key:
                cache.upsert_entity(entity_key, **identity_payload)

            for query_type, provider_endpoint, query_config, query_supported in _query_specs(
                config, query_types, endpoint
            ):
                contract_hash = query_contract_hash(
                    provider_id,
                    config,
                    endpoint=provider_endpoint,
                    query_type=query_type,
                    query_config=query_config,
                )
                endpoint_url = str(
                    query_config.get("endpoint_url")
                    or config.get("endpoint_url")
                    or config.get("api_base")
                    or ""
                )
                if config.get("enabled", True) is False:
                    decision = QueryDecision(
                        "offline_missing",
                        "not_queried",
                        reason="provider disabled by evidence policy",
                        lookup_field=field,
                        lookup_value=value,
                        match_type=match_type,
                    )
                elif resolution.requires_review:
                    decision = QueryDecision(
                        "offline_missing",
                        "identity_review_required",
                        reason="identity conflict requires review before provider lookup",
                        lookup_field=field,
                        lookup_value=value,
                        match_type=match_type,
                    )
                elif not field or not value:
                    reason = (
                        "no resolvable molecular identity"
                        if resolution.status == "audit_missing"
                        else "no provider-compatible molecular identity"
                    )
                    decision = QueryDecision(
                        "offline_missing",
                        "not_queried",
                        reason=reason,
                    )
                elif not query_supported:
                    decision = QueryDecision(
                        "offline_missing",
                        "not_queried",
                        reason=f"unsupported_query_type:{query_type}",
                    )
                elif config.get("live_supported", True) is False:
                    decision = QueryDecision(
                        "offline_missing",
                        "not_queried",
                        reason="local-only adapter unavailable for live candidate lookup",
                    )
                else:
                    decision = cache.decide(
                        source_id=provider_id,
                        entity_key=entity_key,
                        endpoint=provider_endpoint,
                        online=online,
                        force_refresh=force_refresh,
                        expected_adapter_version=str(
                            config.get("adapter_version") or ""
                        ),
                        expected_endpoint_url=endpoint_url,
                        expected_query_contract_hash=contract_hash,
                    )
                dedup_key = (
                    molecule_id,
                    provider_id,
                    provider_endpoint,
                    field or "",
                    value or "",
                )
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                tasks.append(
                    EvidenceQueryTask(
                        provider_id=provider_id,
                        molecule_id=molecule_id,
                        entity_key=entity_key,
                        endpoint=provider_endpoint,
                        lookup_field=field,
                        lookup_value=value,
                        decision=decision,
                        query_type=query_type,
                        match_type=match_type or "",
                        identity_status=resolution.status,
                        endpoint_url=endpoint_url,
                        adapter_version=str(config.get("adapter_version") or ""),
                        query_contract_hash=contract_hash,
                        concurrency=_integer(config.get("concurrency"), 1, minimum=1),
                        timeout_sec=_number(config.get("timeout_sec"), 20.0),
                        rate_limit_per_sec=_number(config.get("rate_limit_per_sec"), 0.0),
                        retry_attempts=_integer(config.get("retry_attempts"), 0),
                        retry_backoff_sec=_number(config.get("retry_backoff_sec"), 0.0),
                        circuit_fail_threshold=_integer(
                            config.get("circuit_fail_threshold"), 3, minimum=1
                        ),
                        circuit_reset_sec=_number(config.get("circuit_reset_sec"), 60.0),
                    )
                )
    return tasks
