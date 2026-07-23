"""Plan local-first evidence enrichment without changing ranking behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from services.evidence_gateway.cache import EvidenceQueryCache, QueryDecision


@dataclass(frozen=True)
class EvidenceQueryTask:
    """One auditable provider lookup planned for a molecule."""

    provider_id: str
    molecule_id: str
    entity_key: str
    endpoint: str
    lookup_field: str | None
    lookup_value: str | None
    decision: QueryDecision

    @property
    def action(self) -> str:
        return self.decision.action


def _identity_value(entity: Mapping[str, Any], identity_order: Iterable[str]) -> tuple[str | None, str | None]:
    for field in identity_order:
        value = entity.get(field)
        if value is not None and str(value).strip():
            return field, str(value).strip()
    return None, None


def plan_provider_queries(
    cache: EvidenceQueryCache,
    entities: Iterable[Mapping[str, Any]],
    providers: Mapping[str, Mapping[str, Any]],
    *,
    online: bool,
    endpoint: str = "identity_lookup",
) -> list[EvidenceQueryTask]:
    """Create deterministic local/remote decisions for SDF entities.

    The function only plans work. Network calls belong to provider adapters and
    must persist their result through ``EvidenceQueryCache.record``.
    """

    tasks: list[EvidenceQueryTask] = []
    for entity in entities:
        molecule_id = str(entity.get("molecule_id") or entity.get("id") or "")
        if not molecule_id:
            continue
        identity_key = (
            entity.get("original_inchikey")
            or entity.get("standardized_inchikey")
            or entity.get("cas")
        )
        entity_key = str(identity_key).strip() if identity_key else molecule_id
        cache.upsert_entity(
            entity_key,
            original_inchikey=entity.get("original_inchikey"),
            standardized_inchikey=entity.get("standardized_inchikey"),
            cas=entity.get("cas"),
            standardized_smiles=entity.get("standardized_smiles"),
        )
        for provider_id, config in providers.items():
            if config.get("enabled", True) is False:
                continue
            identity_order = config.get("identity_order")
            if not identity_order:
                continue
            field, value = _identity_value(entity, identity_order)
            provider_endpoint = str(config.get("endpoint") or endpoint)
            if value is None:
                decision = QueryDecision(
                    "offline_missing",
                    "not_queried",
                    reason="no provider-compatible molecular identity",
                )
            else:
                decision = cache.decide(
                    source_id=provider_id,
                    entity_key=entity_key,
                    endpoint=provider_endpoint,
                    online=online,
                )
            tasks.append(
                EvidenceQueryTask(
                    provider_id=provider_id,
                    molecule_id=molecule_id,
                    entity_key=entity_key,
                    endpoint=provider_endpoint,
                    lookup_field=field,
                    lookup_value=value,
                    decision=decision,
                )
            )
    return tasks
