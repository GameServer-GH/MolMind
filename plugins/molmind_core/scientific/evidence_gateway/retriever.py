"""Bounded, provider-isolated execution for evidence query plans.

Adapters run in worker threads and receive only :class:`EvidenceQueryTask`.
They never receive the cache connection.  The coordinating thread performs all
SQLite reads and writes so the default sqlite3 thread contract is preserved.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import yaml

from packages.models import EvidenceHit
from plugins.molmind_core.scientific.evidence_gateway.cache import EvidenceQueryCache
from plugins.molmind_core.scientific.evidence_gateway.contract import (
    STATUS_PRIORITY,
    aggregate_status,
    canonical_status,
    claim_ceiling,
    content_sha256,
    json_safe,
    lookup_field_family,
    lookup_value_equal,
    redact_text,
)
from plugins.molmind_core.scientific.evidence_gateway.identity import (
    IdentityResolution,
    MoleculeIdentity,
    resolution_from_mapping,
)
from plugins.molmind_core.scientific.evidence_gateway.planner import (
    EvidenceQueryTask,
    plan_provider_queries,
)
from plugins.molmind_core.scientific.paths import REPO_ROOT


Adapter = Callable[[EvidenceQueryTask], list[EvidenceHit]]
EventSink = Callable[[dict[str, Any]], None]

_AUTH_WORDS = (
    "authentication",
    "authorization",
    "unauthorized",
    "forbidden",
    "credential",
    "api key",
    "api_key",
    "token missing",
)
_HIT_FIELDS = {item.name for item in fields(EvidenceHit)}
_EVENT_TYPE_ALIASES = {
    "remote_query_started": "remote_start",
    "remote_query_finished": "remote_end",
    "evidence_summary": "query_summary",
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_text(value: Any) -> str:
    return redact_text(value)


def _safe_payload(value: Any) -> Any:
    return json_safe(value)


def load_provider_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load the evidence-provider policy used by planning and execution."""

    config_path = Path(path) if path is not None else REPO_ROOT / "configs/evidence_providers.yaml"
    return copy.deepcopy(_load_provider_config_cached(_provider_config_cache_key(config_path)))


def _provider_config_cache_key(config_path: Path) -> tuple[str, int | None, int | None]:
    try:
        resolved = config_path.resolve()
    except OSError:
        resolved = config_path
    try:
        stat = resolved.stat()
        return (str(resolved), int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return (str(resolved), None, None)


@lru_cache(maxsize=8)
def _load_provider_config_cached(cache_key: tuple[str, int | None, int | None]) -> dict[str, Any]:
    config_path = Path(cache_key[0])
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError("evidence provider config must be a YAML mapping")
    providers = loaded.get("providers")
    if providers is None:
        loaded["providers"] = {}
    elif not isinstance(providers, dict):
        raise ValueError("evidence provider config 'providers' must be a mapping")
    return loaded


@dataclass(frozen=True)
class RetrievalResult:
    hits_by_molecule: dict[str, list[EvidenceHit]]
    audits_by_molecule: dict[str, list[EvidenceHit]]
    tasks: list[EvidenceQueryTask]
    events: list[dict[str, Any]]
    degraded_channels: list[str]
    identities_by_molecule: dict[str, dict[str, Any]]
    timed_out: bool = False
    cancelled: bool = False


@dataclass
class _CircuitState:
    consecutive_failures: int = 0
    opened_until: float = 0.0


class _RateLimiter:
    def __init__(self, rate_per_sec: float):
        self.interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._next_allowed = 0.0
        self._lock = threading.Lock()

    def wait(
        self,
        *,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            target = max(now, self._next_allowed)
            self._next_allowed = target + self.interval
        delay = target - now
        if delay > 0:
            if deadline is not None:
                delay = min(delay, max(0.0, deadline - time.monotonic()))
            if delay <= 0 or (cancel_event is not None and cancel_event.wait(delay)):
                raise TimeoutError("evidence query cancelled before provider request")


def _is_auth_error(error: BaseException) -> bool:
    status_code = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    if status_code in {401, 403}:
        return True
    name = type(error).__name__.lower()
    message = str(error).lower()
    return "auth" in name or any(word in message for word in _AUTH_WORDS)


def _content_sha(payload: Any) -> str:
    return content_sha256(payload)


def _lookup_field_family(value: str) -> str:
    return lookup_field_family(value)


def _lookup_value_equal(field: str, left: str, right: str) -> bool:
    return lookup_value_equal(field, left, right)


def _resolution_for(
    value: Mapping[str, Any] | MoleculeIdentity | IdentityResolution,
) -> IdentityResolution:
    if isinstance(value, IdentityResolution):
        return value
    if isinstance(value, MoleculeIdentity):
        return resolution_from_mapping(value.to_dict())
    if isinstance(value.get("identity"), Mapping):
        nested = dict(value["identity"])
        nested.setdefault("molecule_id", value.get("molecule_id") or value.get("id"))
        resolution = resolution_from_mapping(nested)
        if str(value.get("status") or "") == "identity_review_required":
            return IdentityResolution(
                identity=resolution.identity,
                status="identity_review_required",
                candidates=resolution.candidates,
                lookup_field=resolution.lookup_field,
                lookup_value=resolution.lookup_value,
                match_type=resolution.match_type,
                conflicts=tuple(str(item) for item in value.get("conflicts") or ()),
                notes=resolution.notes,
            )
        return resolution
    return resolution_from_mapping(value)


def _deserialize_hits(payload: Any) -> list[EvidenceHit]:
    rows = payload.get("hits") if isinstance(payload, Mapping) else payload
    if not isinstance(rows, list):
        return []
    hits: list[EvidenceHit] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        values = {key: value for key, value in row.items() if key in _HIT_FIELDS}
        try:
            hits.append(EvidenceHit(**values))
        except (TypeError, ValueError):
            continue
    return hits


class EvidenceRetriever:
    """Execute a local-first plan with deterministic aggregation."""

    def __init__(
        self,
        cache: EvidenceQueryCache,
        provider_configs: Mapping[str, Any],
        adapters: Mapping[str, Adapter],
    ):
        self.cache = cache
        if isinstance(provider_configs.get("providers"), Mapping):
            self._full_config = dict(provider_configs)
            self.provider_configs = dict(provider_configs["providers"])
        else:
            self._full_config = {"providers": dict(provider_configs)}
            self.provider_configs = dict(provider_configs)
        self.cache.configure(self._full_config.get("cache"))
        self.adapters = dict(adapters)
        self._circuits: dict[str, _CircuitState] = {
            provider_id: _CircuitState() for provider_id in self.provider_configs
        }
        self._rate_limiters: dict[str, _RateLimiter] = {}

    def _emit(
        self,
        events: list[dict[str, Any]],
        sink: EventSink | None,
        event_type: str,
        **payload: Any,
    ) -> None:
        safe = _safe_payload(payload)
        canonical_type = _EVENT_TYPE_ALIASES.get(event_type, event_type)
        event = {
            "type": canonical_type,
            "event": event_type,
            "timestamp": _now(),
            **safe,
        }
        # Agent/Web use the shorter aliases while the gateway contract keeps
        # explicit provider/query field names.  Emit both without credentials.
        if "provider_id" in event and "provider" not in event:
            event["provider"] = event["provider_id"]
        if "query_status" in event and "status" not in event:
            event["status"] = event["query_status"]
        if "evidence_count" in event and "hit_count" not in event:
            event["hit_count"] = event["evidence_count"]
        events.append(event)
        if sink is not None:
            try:
                sink(dict(event))
            except Exception:
                # A UI callback is observability only and cannot alter evidence
                # retrieval or its deterministic output.
                pass

    def _audit_hit(
        self,
        task: EvidenceQueryTask,
        status: str,
        reason: str,
        *,
        retrieved_at: str = "",
        response_sha256: str = "",
    ) -> EvidenceHit:
        audit_payload: dict[str, Any] = {
            "action": task.action,
            "reason": _safe_text(reason),
            "requested_query_types": task.query_type.split(","),
        }
        if task.decision.prior_status:
            audit_payload["cached_status"] = task.decision.prior_status
        if task.decision.retrieved_at:
            audit_payload["cached_retrieved_at"] = task.decision.retrieved_at
        if task.decision.expires_at:
            audit_payload["cache_expires_at"] = task.decision.expires_at
        if task.decision.next_retry_at:
            audit_payload["next_retry_at"] = task.decision.next_retry_at
        if status == "verified_empty":
            audit_payload["verified_empty_is_not_biological_negative"] = True
        if response_sha256:
            audit_payload["response_sha256_basis"] = "cached_or_current_provider_payload"
        else:
            response_sha256 = _content_sha(
                {
                    "provider_id": task.provider_id,
                    "endpoint": task.endpoint,
                    "lookup_field": task.lookup_field,
                    "lookup_value": task.lookup_value,
                    "status": status,
                    "audit": audit_payload,
                }
            )
            audit_payload["response_sha256_basis"] = "query_audit_payload"
        identity_seed = "|".join(
            (
                task.molecule_id,
                task.provider_id,
                task.endpoint,
                task.lookup_field or "",
                task.lookup_value or "",
                status,
            )
        )
        return EvidenceHit(
            adapter_id=task.provider_id,
            provider_id=task.provider_id,
            query_type="query_audit",
            score=0.0,
            confidence=0.0,
            evidence_id="query-audit:" + hashlib.sha256(identity_seed.encode()).hexdigest()[:24],
            payload=audit_payload,
            endpoint=task.endpoint,
            direction="unknown",
            evidence_role="query_audit",
            provenance_status="audited",
            source_url=_safe_text(task.endpoint_url),
            retrieved_at=retrieved_at or _now(),
            adapter_version=task.adapter_version or "gateway-query-contract-v2",
            source_version=task.adapter_version or "gateway-query-state-v2",
            response_sha256=response_sha256,
            query_status=status,  # type: ignore[arg-type]
            evidence_type="query_audit",
            lookup_field=task.lookup_field or "",
            lookup_value=task.lookup_value or "",
            match_type=task.match_type,
            accession=f"{task.provider_id}:{task.endpoint}",
            claim_ceiling="transport_status_only",
        )

    @staticmethod
    def _canonical_hit_status(hit: EvidenceHit) -> str:
        return canonical_status(hit.query_status, hit)

    def _filter_requested_hits(
        self, task: EvidenceQueryTask, hits: Iterable[EvidenceHit]
    ) -> list[EvidenceHit]:
        requested_types = {
            item for item in task.query_type.split(",") if item and item != "identity"
        }
        if not requested_types:
            return list(hits)
        return [
            hit
            for hit in hits
            if hit.query_type in requested_types
            # Provider audit and identity annotation must survive endpoint
            # filtering; otherwise an empty filtered list becomes a false
            # ``verified_empty`` transport result.
            or hit.evidence_role in {"query_audit", "annotation_only"}
            or hit.query_status in {"identity_review_required", "annotation_only"}
        ]

    def _aggregate_status(self, hits: Sequence[EvidenceHit]) -> str:
        return aggregate_status(hits)

    def _normalize_hits(
        self, task: EvidenceQueryTask, raw_hits: Iterable[EvidenceHit]
    ) -> list[EvidenceHit]:
        normalized: list[EvidenceHit] = []
        for index, original in enumerate(raw_hits):
            if not isinstance(original, EvidenceHit):
                raise TypeError("provider adapter must return EvidenceHit instances")
            hit = EvidenceHit(**asdict(original))
            hit.raw_status = hit.raw_status or str(hit.query_status or "")
            reported_provider = str(hit.provider_id or "").strip()
            reported_field = str(hit.lookup_field or "").strip()
            reported_value = str(hit.lookup_value or "").strip()
            provider_conflict = bool(
                reported_provider and reported_provider != task.provider_id
            )
            field_conflict = bool(
                reported_field
                and _lookup_field_family(reported_field)
                != _lookup_field_family(task.lookup_field or "")
            )
            value_conflict = bool(
                reported_value
                and task.lookup_value
                and not _lookup_value_equal(
                    task.lookup_field or reported_field,
                    reported_value,
                    task.lookup_value,
                )
            )
            if provider_conflict or field_conflict or value_conflict:
                original_payload = _safe_payload(hit.payload)
                hit.payload = {
                    "reason": "provider_lookup_metadata_conflict",
                    "planned_provider": task.provider_id,
                    "planned_lookup_field": task.lookup_field or "",
                    "planned_lookup_value": task.lookup_value or "",
                    "reported_provider": reported_provider,
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
                    "query-audit:provider-identity:"
                    + _content_sha(
                        {
                            "provider": task.provider_id,
                            "lookup_field": task.lookup_field,
                            "lookup_value": task.lookup_value,
                            "reported_provider": reported_provider,
                            "reported_field": reported_field,
                            "reported_value": reported_value,
                            "original_evidence_id": hit.evidence_id,
                        }
                    )[:24]
                )
            hit.adapter_id = hit.adapter_id or task.provider_id
            hit.provider_id = task.provider_id
            hit.endpoint = hit.endpoint or task.endpoint
            hit.lookup_field = task.lookup_field or reported_field
            hit.lookup_value = task.lookup_value or reported_value
            hit.match_type = (
                "provider_lookup_metadata_conflict"
                if provider_conflict or field_conflict or value_conflict
                else (hit.match_type or task.match_type)
            )
            hit.adapter_version = hit.adapter_version or task.adapter_version
            hit.source_version = hit.source_version or hit.adapter_version
            hit.source_url = _safe_text(hit.source_url or task.endpoint_url)
            hit.payload = _safe_payload(hit.payload)
            hit.query_params = _safe_payload(hit.query_params)
            hit.retrieved_at = hit.retrieved_at or _now()
            if not hit.evidence_id:
                seed = {
                    "provider": task.provider_id,
                    "endpoint": task.endpoint,
                    "lookup": task.lookup_value,
                    "index": index,
                    "payload": hit.payload,
                }
                hit.evidence_id = "evidence:" + _content_sha(seed)[:24]
            if not hit.response_sha256:
                hit.response_sha256 = _content_sha(hit.payload)

            # Canonicalize the provider status before interpreting its role or
            # numeric values.  Incoherent combinations fail closed at the
            # gateway boundary so direct Retriever callers receive the same
            # scientific safeguards as the Agent/Tool wrapper.
            canonical_status = self._canonical_hit_status(hit)
            if canonical_status == "annotation_only" or hit.evidence_role == "annotation_only":
                hit.query_status = "annotation_only"
                hit.evidence_role = "annotation_only"
                hit.score = 0.0
                hit.confidence = 0.0
                if hit.evidence_type == "unresolved":
                    hit.evidence_type = "identity_annotation"
            elif canonical_status in {
                "verified_empty",
                "query_failed",
                "auth_missing",
                "not_queried",
                "identity_review_required",
            }:
                if hit.evidence_role != "query_audit":
                    hit.payload = {
                        "reason": "provider_status_not_scientific_evidence",
                        "provider_query_status": canonical_status,
                        "provider_payload": _safe_payload(hit.payload),
                    }
                hit.query_status = canonical_status  # type: ignore[assignment]
                hit.query_type = "query_audit"
                hit.evidence_role = "query_audit"
                hit.evidence_type = "query_audit"
                hit.score = 0.0
                hit.confidence = 0.0
                hit.direction = "unknown"
                hit.claim_ceiling = "transport_status_only"
            elif hit.evidence_role == "query_audit":
                # A successful endpoint audit remains an audit, never a score.
                hit.query_status = "hit"
                hit.score = 0.0
                hit.confidence = 0.0
                hit.evidence_type = "query_audit"
            else:
                hit.query_status = "hit"

            try:
                hit.score = float(hit.score)
                hit.confidence = float(hit.confidence)
            except (TypeError, ValueError) as error:
                raise ValueError("provider score/confidence must be numeric") from error
            if not math.isfinite(hit.score) or not math.isfinite(hit.confidence):
                raise ValueError("provider score/confidence must be finite")
            if not (0.0 <= hit.score <= 1.0) or not (0.0 <= hit.confidence <= 1.0):
                raise ValueError("provider score/confidence must be within [0, 1]")
            try:
                json.dumps(
                    {"payload": hit.payload, "query_params": hit.query_params},
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
            except (TypeError, ValueError) as error:
                raise ValueError("provider payload must be JSON serializable") from error
            hit.claim_ceiling = claim_ceiling(hit)
            normalized.append(hit)
        return normalized

    def _execute_adapter(
        self,
        task: EvidenceQueryTask,
        adapter: Adapter,
        limiter: _RateLimiter,
        cancel_event: threading.Event,
        deadline: float | None,
    ) -> list[EvidenceHit]:
        last_error: Exception | None = None
        for attempt in range(task.retry_attempts + 1):
            if cancel_event.is_set() or (
                deadline is not None and time.monotonic() >= deadline
            ):
                raise TimeoutError("evidence query total deadline exceeded")
            limiter.wait(cancel_event=cancel_event, deadline=deadline)
            try:
                result = adapter(task)
                if result is None:
                    return []
                return self._normalize_hits(task, result)
            except Exception as error:
                last_error = error
                if _is_auth_error(error) or attempt >= task.retry_attempts:
                    raise
                if task.retry_backoff_sec > 0:
                    delay = task.retry_backoff_sec * (2**attempt)
                    if deadline is not None:
                        delay = min(delay, max(0.0, deadline - time.monotonic()))
                    if delay <= 0 or cancel_event.wait(delay):
                        raise TimeoutError("evidence query cancelled during retry backoff")
        assert last_error is not None
        raise last_error

    def query(
        self,
        identities: Iterable[Mapping[str, Any] | MoleculeIdentity | IdentityResolution],
        providers: Sequence[str] | None = None,
        query_types: Sequence[str] | None = None,
        allow_live: bool = False,
        force_refresh: bool = False,
        event_sink: EventSink | None = None,
        total_timeout_sec: float | None = None,
        deadline: float | datetime | None = None,
        cancel_event: threading.Event | None = None,
    ) -> RetrievalResult:
        started_monotonic = time.monotonic()
        if total_timeout_sec is not None:
            total_deadline = started_monotonic + max(0.0, float(total_timeout_sec))
        elif isinstance(deadline, datetime):
            target = deadline
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            total_deadline = started_monotonic + max(
                0.0, (target - datetime.now(timezone.utc)).total_seconds()
            )
        elif deadline is not None:
            total_deadline = float(deadline)
        else:
            total_deadline = None
        cancellation = cancel_event or threading.Event()
        timed_out = False
        values = list(identities)
        resolutions = [_resolution_for(value) for value in values]
        tasks = plan_provider_queries(
            self.cache,
            resolutions,
            self.provider_configs,
            online=allow_live,
            providers=providers,
            query_types=query_types,
            force_refresh=force_refresh,
        )

        events: list[dict[str, Any]] = []
        hits_by_molecule: dict[str, list[EvidenceHit]] = {}
        audits_by_molecule: dict[str, list[EvidenceHit]] = {}
        identities_by_molecule: dict[str, dict[str, Any]] = {}
        degraded_channels: list[str] = []
        for index, resolution in enumerate(resolutions):
            molecule_id = resolution.molecule_id or resolution.lookup_value or f"entity:{index}"
            hits_by_molecule.setdefault(molecule_id, [])
            audits_by_molecule.setdefault(molecule_id, [])
            identities_by_molecule[molecule_id] = resolution.to_dict()
            if resolution.requires_review:
                self._emit(
                    events,
                    event_sink,
                    "identity_conflict",
                    molecule_id=molecule_id,
                    conflicts=list(resolution.conflicts),
                )

        self._emit(
            events,
            event_sink,
            "query_plan",
            allow_live=allow_live,
            force_refresh=force_refresh,
            task_count=len(tasks),
            tasks=[
                {
                    "provider_id": task.provider_id,
                    "molecule_id": task.molecule_id,
                    "query_types": task.query_type.split(","),
                    "endpoint": task.endpoint,
                    "lookup_field": task.lookup_field,
                    "lookup_value": task.lookup_value,
                    "match_type": task.match_type,
                    "action": task.action,
                }
                for task in tasks
            ],
        )

        # Results are keyed by request identity, not molecule_id, so aliases in
        # one run share exactly one adapter call.
        request_results: dict[
            tuple[str, str, str, str, str, str], tuple[str, list[EvidenceHit], str]
        ] = {}
        remote_representatives: list[EvidenceQueryTask] = []
        seen_remote: set[tuple[str, str, str, str, str, str]] = set()

        for task in tasks:
            if task.action == "local_hit":
                payload = self.cache.load_payload(
                    source_id=task.provider_id,
                    entity_key=task.entity_key,
                    endpoint=task.endpoint,
                    query_contract_hash=task.query_contract_hash,
                )
                cached_hits = self._normalize_hits(task, _deserialize_hits(payload))
                hits = self._filter_requested_hits(task, cached_hits)
                cached_transport_failures = [
                    self._canonical_hit_status(hit)
                    for hit in cached_hits
                    if hit.evidence_role == "query_audit"
                    and self._canonical_hit_status(hit)
                    in {"query_failed", "auth_missing"}
                ]
                if cached_transport_failures:
                    degraded_status = (
                        "auth_missing"
                        if "auth_missing" in cached_transport_failures
                        else "query_failed"
                    )
                    if task.provider_id not in degraded_channels:
                        degraded_channels.append(task.provider_id)
                    self._emit(
                        events,
                        event_sink,
                        "degraded",
                        provider_id=task.provider_id,
                        molecule_id=task.molecule_id,
                        endpoint=task.endpoint,
                        query_status=degraded_status,
                        error="cached provider payload contains a degraded channel audit",
                    )
                state = self.cache.get_state(
                    source_id=task.provider_id,
                    entity_key=task.entity_key,
                    endpoint=task.endpoint,
                    query_contract_hash=task.query_contract_hash,
                ) or {}
                cached_status = str(task.decision.status)
                reason = str(state.get("error_message") or task.decision.reason)
                if cached_status in {"hit", "annotation_only"} and not cached_hits:
                    # A state row without replayable normalized EvidenceHit
                    # objects is not a hit.  In live mode repair it through the
                    # same bounded provider executor; offline mode reports a
                    # structured cache failure instead of false success.
                    request_results[task.request_key] = (
                        "query_failed",
                        [],
                        "cached normalized payload is unavailable or invalid",
                    )
                    self._emit(
                        events,
                        event_sink,
                        "degraded",
                        provider_id=task.provider_id,
                        molecule_id=task.molecule_id,
                        endpoint=task.endpoint,
                        query_status="query_failed",
                        error="cached normalized payload is unavailable or invalid",
                    )
                    if allow_live and task.request_key not in seen_remote:
                        seen_remote.add(task.request_key)
                        remote_representatives.append(task)
                    elif task.provider_id not in degraded_channels:
                        degraded_channels.append(task.provider_id)
                    continue
                if cached_status == "hit":
                    # Cache identity is provider+entity+endpoint. A previous
                    # lipid hit cannot be replayed as a tox hit merely because
                    # both share the same endpoint.
                    visible_status = self._aggregate_status(hits)
                    if visible_status == "verified_empty":
                        reason = (
                            "cached endpoint payload contains no records for "
                            "the requested query types; not a biological negative label"
                        )
                    cached_status = visible_status
                request_results[task.request_key] = (
                    cached_status,
                    hits,
                    reason,
                )
                self._emit(
                    events,
                    event_sink,
                    "local_hit",
                    provider_id=task.provider_id,
                    molecule_id=task.molecule_id,
                    endpoint=task.endpoint,
                    query_status=cached_status,
                    evidence_count=len(hits),
                )
            elif task.action == "skip_fresh_verified_empty":
                payload = self.cache.load_payload(
                    source_id=task.provider_id,
                    entity_key=task.entity_key,
                    endpoint=task.endpoint,
                    query_contract_hash=task.query_contract_hash,
                )
                cached_hits = self._normalize_hits(task, _deserialize_hits(payload))
                request_results[task.request_key] = (
                    "verified_empty",
                    self._filter_requested_hits(task, cached_hits),
                    task.decision.reason,
                )
            elif task.action in {"query_remote", "retry_remote"}:
                if task.request_key not in seen_remote:
                    seen_remote.add(task.request_key)
                    remote_representatives.append(task)

        provider_tasks: dict[str, list[EvidenceQueryTask]] = {}
        for task in remote_representatives:
            provider_tasks.setdefault(task.provider_id, []).append(task)

        executors: dict[str, ThreadPoolExecutor] = {}
        futures: dict[
            tuple[str, str, str, str, str, str], Future[list[EvidenceHit]]
        ] = {}
        deadlines: dict[tuple[str, str, str, str, str, str], float] = {}
        immediate_errors: dict[
            tuple[str, str, str, str, str, str], Exception
        ] = {}
        provider_next_index: dict[str, int] = {}
        provider_adapters: dict[str, Adapter] = {}
        provider_limiters: dict[str, _RateLimiter] = {}

        def submit_remote(task: EvidenceQueryTask) -> None:
            nonlocal timed_out
            remaining_total = (
                None
                if total_deadline is None
                else max(0.0, total_deadline - time.monotonic())
            )
            if cancellation.is_set() or remaining_total == 0.0:
                timed_out = timed_out or remaining_total == 0.0
                raise TimeoutError("evidence query total deadline exceeded or cancelled")
            effective_timeout = task.timeout_sec
            if remaining_total is not None:
                effective_timeout = min(effective_timeout, remaining_total)
            dispatched_task = replace(task, timeout_sec=max(0.001, effective_timeout))
            provider_id = task.provider_id
            self._emit(
                events,
                event_sink,
                "remote_query_started",
                provider_id=provider_id,
                molecule_id=task.molecule_id,
                endpoint=task.endpoint,
                query_types=task.query_type.split(","),
                allow_live=True,
            )
            futures[task.request_key] = executors[provider_id].submit(
                self._execute_adapter,
                dispatched_task,
                provider_adapters[provider_id],
                provider_limiters[provider_id],
                cancellation,
                total_deadline,
            )
            # At most ``concurrency`` tasks are submitted, so this deadline
            # starts at actual dispatch rather than while waiting in an
            # unbounded executor queue.
            deadlines[task.request_key] = time.monotonic() + effective_timeout

        def stop_unsent(provider_id: str, error: Exception) -> None:
            grouped = provider_tasks.get(provider_id, [])
            index = provider_next_index.get(provider_id, len(grouped))
            for pending in grouped[index:]:
                immediate_errors[pending.request_key] = error
            provider_next_index[provider_id] = len(grouped)

        def advance_provider(provider_id: str) -> None:
            grouped = provider_tasks.get(provider_id, [])
            index = provider_next_index.get(provider_id, len(grouped))
            if index >= len(grouped) or provider_id not in executors:
                return
            if cancellation.is_set() or (
                total_deadline is not None and time.monotonic() >= total_deadline
            ):
                stop_unsent(
                    provider_id,
                    TimeoutError("evidence query total deadline exceeded or cancelled"),
                )
                return
            circuit = self._circuits.setdefault(provider_id, _CircuitState())
            if circuit.opened_until > time.monotonic():
                stop_unsent(
                    provider_id,
                    RuntimeError(f"provider circuit open: {provider_id}"),
                )
                return
            try:
                submit_remote(grouped[index])
                provider_next_index[provider_id] = index + 1
            except TimeoutError as error:
                immediate_errors[grouped[index].request_key] = error
                stop_unsent(provider_id, error)

        try:
            for provider_id, grouped in provider_tasks.items():
                representative = grouped[0]
                adapter = self.adapters.get(provider_id)
                if adapter is None:
                    error = RuntimeError(f"provider adapter unavailable: {provider_id}")
                    for task in grouped:
                        immediate_errors[task.request_key] = error
                    provider_next_index[provider_id] = len(grouped)
                    continue
                state = self._circuits.setdefault(provider_id, _CircuitState())
                now = time.monotonic()
                if state.opened_until > now:
                    error = RuntimeError(f"provider circuit open: {provider_id}")
                    for task in grouped:
                        immediate_errors[task.request_key] = error
                    provider_next_index[provider_id] = len(grouped)
                    continue
                limiter = self._rate_limiters.get(provider_id)
                if limiter is None:
                    limiter = _RateLimiter(representative.rate_limit_per_sec)
                    self._rate_limiters[provider_id] = limiter
                executor = ThreadPoolExecutor(
                    max_workers=max(1, representative.concurrency),
                    thread_name_prefix=f"evidence-{provider_id}",
                )
                executors[provider_id] = executor
                provider_adapters[provider_id] = adapter
                provider_limiters[provider_id] = limiter
                initial = min(max(1, representative.concurrency), len(grouped))
                provider_next_index[provider_id] = initial
                for task in grouped[:initial]:
                    try:
                        submit_remote(task)
                    except TimeoutError as error:
                        cancellation.set()
                        timed_out = (
                            total_deadline is not None
                            and time.monotonic() >= total_deadline
                        )
                        immediate_errors[task.request_key] = error
                        stop_unsent(provider_id, error)
                        break

            # Consume in plan order, independently of completion order.  This
            # fixes output ordering while all providers still run concurrently.
            for task in remote_representatives:
                key = task.request_key
                error = immediate_errors.get(key)
                hits: list[EvidenceHit] = []
                if error is None:
                    if cancellation.is_set() or (
                        total_deadline is not None
                        and time.monotonic() >= total_deadline
                    ):
                        cancellation.set()
                        timed_out = total_deadline is not None
                        error = TimeoutError(
                            "evidence query total deadline exceeded or cancelled"
                        )
                    future = futures.get(key)
                    if error is not None:
                        if future is not None:
                            future.cancel()
                    elif future is None:
                        error = RuntimeError(
                            f"provider scheduler produced no request: {task.provider_id}"
                        )
                    else:
                        remaining = max(0.0, deadlines[key] - time.monotonic())
                        if total_deadline is not None:
                            remaining = min(
                                remaining,
                                max(0.0, total_deadline - time.monotonic()),
                            )
                        try:
                            hits = future.result(timeout=remaining)
                        except FutureTimeout:
                            future.cancel()
                            if (
                                total_deadline is not None
                                and time.monotonic() >= total_deadline
                            ):
                                cancellation.set()
                                timed_out = True
                            error = TimeoutError(
                                "evidence query total deadline exceeded"
                                if timed_out
                                else f"provider request timed out after {task.timeout_sec:g}s"
                            )
                        except Exception as caught:
                            error = caught

                if error is None:
                    status = self._aggregate_status(hits)
                    payload = [asdict(hit) for hit in hits]
                    self.cache.record(
                        source_id=task.provider_id,
                        entity_key=task.entity_key,
                        endpoint=task.endpoint,
                        status=status,  # type: ignore[arg-type]
                        # Keep provider-supplied query audits and annotations so
                        # cache replay does not collapse them into a generic
                        # empty result.
                        payload=payload if hits else None,
                        source_version=(
                            next((hit.source_version for hit in hits if hit.source_version), "")
                            or task.adapter_version
                        ),
                        lookup_field=task.lookup_field,
                        lookup_value=task.lookup_value,
                        match_type=task.match_type,
                        endpoint_url=task.endpoint_url,
                        adapter_version=task.adapter_version,
                        query_type=task.query_type,
                        query_contract_hash=task.query_contract_hash,
                    )
                    state = self.cache.get_state(
                        source_id=task.provider_id,
                        entity_key=task.entity_key,
                        endpoint=task.endpoint,
                        query_contract_hash=task.query_contract_hash,
                    ) or {}
                    visible_hits = self._filter_requested_hits(task, hits)
                    reason = {
                        "hit": "provider returned normalized scientific evidence",
                        "annotation_only": "provider returned identity or mechanism annotation only",
                        "verified_empty": "provider returned no records; not a biological negative label",
                        "identity_review_required": "provider result requires identity review",
                        "auth_missing": "provider reported missing authorization",
                        "query_failed": "provider reported a structured query failure",
                        "not_queried": "provider reported that no query was performed",
                    }.get(status, "provider query completed")
                    request_results[key] = (status, visible_hits, reason)
                    circuit = self._circuits.setdefault(task.provider_id, _CircuitState())
                    transport_failures = [
                        self._canonical_hit_status(hit)
                        for hit in hits
                        if hit.evidence_role == "query_audit"
                        and self._canonical_hit_status(hit)
                        in {"query_failed", "auth_missing"}
                    ]
                    failure_status = (
                        "auth_missing"
                        if "auth_missing" in transport_failures
                        else ("query_failed" if transport_failures else "")
                    )
                    if status in {"query_failed", "auth_missing"} or failure_status:
                        degraded_status = failure_status or status
                        if task.provider_id not in degraded_channels:
                            degraded_channels.append(task.provider_id)
                        circuit.consecutive_failures += 1
                        if circuit.consecutive_failures >= task.circuit_fail_threshold:
                            circuit.opened_until = time.monotonic() + task.circuit_reset_sec
                        self._emit(
                            events,
                            event_sink,
                            "degraded",
                            provider_id=task.provider_id,
                            molecule_id=task.molecule_id,
                            endpoint=task.endpoint,
                            query_status=degraded_status,
                            error=reason,
                        )
                    else:
                        # Do not let an already in-flight success immediately
                        # close a circuit opened by another request in the same
                        # provider wave.
                        if circuit.opened_until <= time.monotonic():
                            circuit.consecutive_failures = 0
                            circuit.opened_until = 0.0
                    if status == "identity_review_required":
                        self._emit(
                            events,
                            event_sink,
                            "identity_conflict",
                            provider_id=task.provider_id,
                            molecule_id=task.molecule_id,
                            endpoint=task.endpoint,
                            query_status=status,
                        )
                    self._emit(
                        events,
                        event_sink,
                        "remote_query_finished",
                        provider_id=task.provider_id,
                        molecule_id=task.molecule_id,
                        endpoint=task.endpoint,
                        query_status=status,
                        evidence_count=len(
                            [
                                hit
                                for hit in visible_hits
                                if hit.evidence_role != "query_audit"
                            ]
                        ),
                        response_sha256=str(state.get("payload_sha256") or ""),
                    )
                else:
                    was_submitted = key in futures
                    circuit_blocked = (
                        not was_submitted
                        and "circuit open" in str(error).lower()
                    )
                    status = (
                        "not_queried"
                        if circuit_blocked
                        else ("auth_missing" if _is_auth_error(error) else "query_failed")
                    )
                    safe_error = _safe_text(error)
                    self.cache.record(
                        source_id=task.provider_id,
                        entity_key=task.entity_key,
                        endpoint=task.endpoint,
                        status=status,  # type: ignore[arg-type]
                        error=error if isinstance(error, Exception) else RuntimeError(safe_error),
                        lookup_field=task.lookup_field,
                        lookup_value=task.lookup_value,
                        match_type=task.match_type,
                        endpoint_url=task.endpoint_url,
                        adapter_version=task.adapter_version,
                        query_type=task.query_type,
                        query_contract_hash=task.query_contract_hash,
                    )
                    request_results[key] = (status, [], safe_error)
                    if task.provider_id not in degraded_channels:
                        degraded_channels.append(task.provider_id)
                    circuit = self._circuits.setdefault(task.provider_id, _CircuitState())
                    if not circuit_blocked:
                        circuit.consecutive_failures += 1
                        if circuit.consecutive_failures >= task.circuit_fail_threshold:
                            circuit.opened_until = time.monotonic() + task.circuit_reset_sec
                    if isinstance(error, TimeoutError):
                        # Python cannot kill a running worker thread.  Stop all
                        # unsent work for this provider so a timed-out adapter
                        # cannot accumulate a background request queue.
                        circuit.opened_until = max(
                            circuit.opened_until,
                            time.monotonic() + task.circuit_reset_sec,
                        )
                    self._emit(
                        events,
                        event_sink,
                        "degraded",
                        provider_id=task.provider_id,
                        molecule_id=task.molecule_id,
                        endpoint=task.endpoint,
                        query_status=status,
                        error=safe_error,
                    )
                    if was_submitted:
                        self._emit(
                            events,
                            event_sink,
                            "remote_query_finished",
                            provider_id=task.provider_id,
                            molecule_id=task.molecule_id,
                            endpoint=task.endpoint,
                            query_status=status,
                            evidence_count=0,
                        )
                advance_provider(task.provider_id)
        finally:
            # Do not wait for a timed-out provider.  Workers never touch SQLite.
            for executor in executors.values():
                executor.shutdown(wait=False, cancel_futures=True)

        # Re-associate request-level results to each molecule in the original
        # task order.  Audit rows never enter the scientific hit collection.
        seen_hit_ids: dict[str, set[str]] = {
            molecule_id: set() for molecule_id in hits_by_molecule
        }
        for task in tasks:
            if task.action in {
                "query_remote",
                "retry_remote",
                "local_hit",
                "skip_fresh_verified_empty",
            }:
                status, hits, reason = request_results.get(
                    task.request_key,
                    ("query_failed", [], "planned query produced no result"),
                )
            else:
                status = str(task.decision.status)
                hits = []
                reason = task.decision.reason
            for hit in hits:
                if hit.evidence_role == "query_audit":
                    audits_by_molecule.setdefault(task.molecule_id, []).append(hit)
                    continue
                if hit.evidence_id in seen_hit_ids.setdefault(task.molecule_id, set()):
                    continue
                seen_hit_ids[task.molecule_id].add(hit.evidence_id)
                hits_by_molecule.setdefault(task.molecule_id, []).append(hit)
            state = self.cache.get_state(
                source_id=task.provider_id,
                entity_key=task.entity_key,
                endpoint=task.endpoint,
                query_contract_hash=task.query_contract_hash,
            ) or {}
            # A failed refresh preserves the last good cached payload by
            # design.  Never label that old success hash as the response hash
            # of the current failure/not-queried attempt.
            response_sha256 = (
                str(state.get("payload_sha256") or "")
                if status
                not in {"query_failed", "auth_missing", "not_queried"}
                else ""
            )
            audits_by_molecule.setdefault(task.molecule_id, []).append(
                self._audit_hit(
                    task,
                    status,
                    reason,
                    retrieved_at=str(
                        state.get("retrieved_at") or task.decision.retrieved_at or ""
                    ),
                    response_sha256=response_sha256,
                )
            )
            if status in {"query_failed", "auth_missing"} and task.provider_id not in degraded_channels:
                degraded_channels.append(task.provider_id)

        self._emit(
            events,
            event_sink,
            "evidence_summary",
            molecule_count=len(hits_by_molecule),
            evidence_count=sum(len(items) for items in hits_by_molecule.values()),
            audit_count=sum(len(items) for items in audits_by_molecule.values()),
            degraded_channels=list(degraded_channels),
        )
        return RetrievalResult(
            hits_by_molecule=hits_by_molecule,
            audits_by_molecule=audits_by_molecule,
            tasks=tasks,
            events=events,
            degraded_channels=degraded_channels,
            identities_by_molecule=identities_by_molecule,
            timed_out=timed_out,
            cancelled=cancellation.is_set() and not timed_out,
        )
