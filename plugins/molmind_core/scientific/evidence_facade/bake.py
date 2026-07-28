"""把 ChEMBL / PubChem 证据烘焙为本地 JSONL 快照。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from rdkit import Chem

from packages.chem_core import compute_descriptors, morgan_fp
from packages.goldset import load_goldset
from packages.models import EvidenceHit, MoleculeRecord
from plugins.molmind_core.scientific.evidence_facade.facade import EVIDENCE_SCHEMA_VERSION, EvidenceFacade
from plugins.molmind_core.scientific.evidence_gateway.cache import EvidenceQueryCache
from plugins.molmind_core.scientific.evidence_gateway.contract import (
    CANONICAL_STATUSES,
    canonical_status,
    json_safe,
)
from plugins.molmind_core.scientific.evidence_gateway.identity import resolve_identity
from plugins.molmind_core.scientific.evidence_gateway.retriever import (
    EvidenceRetriever,
    load_provider_config,
)
from plugins.molmind_core.scientific.hard_filter import apply_hard_filters
from plugins.molmind_core.scientific.ingest import parse_sdf_detailed, quiet_rdkit
from plugins.molmind_core.scientific.paths import REPO_ROOT
from plugins.molmind_core.scientific.pipeline.config_loader import SNAPSHOT_DIR, AppConfig, load_config
from plugins.molmind_core.scientific.ranker import score_molecule


ProviderAdapter = Callable[[Any], list[EvidenceHit]]
_DEFAULT_BAKE_PROVIDERS = ("chembl", "pubchem")
_FAILED_QUERY_STATUSES = {
    "query_failed",
    "auth_missing",
    "not_queried",
    "identity_review_required",
}


@dataclass
class BakeStats:
    candidates: int
    fetched: int
    skipped_cached: int
    wrote_rows: int
    failures: int
    output_path: str
    manifest_path: str
    snapshot_sha256: str


@dataclass
class PromoteStats:
    rows: int
    rejected: int
    output_path: str
    manifest_path: str
    snapshot_sha256: str
    dry_run: bool
    diff: dict[str, Any]


def _hit_to_row(
    hit: EvidenceHit,
    *,
    inchikey: str,
    cas: str | None,
    molecule_id: str = "",
    standardized_inchikey: str = "",
    original_inchikey: str | None = None,
) -> dict:
    # Transport state is audit material only.  Enforce the isolation again at
    # the freeze boundary even though EvidenceRetriever already normalizes it.
    query_audit = hit.evidence_role == "query_audit" or hit.query_type == "query_audit"
    query_status = str(hit.query_status or "not_queried")
    provenance_status = str(hit.provenance_status or "audited")
    if query_audit and query_status in {"query_failed", "auth_missing", "not_queried"}:
        # EvidenceFacade deliberately ignores failed frozen rows on replay so
        # they cannot become sticky pseudo-hits. Retry/backoff lives in the
        # Gateway SQLite state instead.
        provenance_status = "query_failed"
    return {
        "molecule_id": molecule_id,
        "inchikey": inchikey,
        "original_inchikey": inchikey if original_inchikey is None else original_inchikey,
        "standardized_inchikey": standardized_inchikey,
        "cas": cas or "",
        "adapter_id": hit.adapter_id,
        "provider_id": hit.provider_id,
        "query_type": hit.query_type,
        "score": 0.0 if query_audit else hit.score,
        "confidence": 0.0 if query_audit else hit.confidence,
        "evidence_id": hit.evidence_id,
        "payload": hit.payload,
        "endpoint": hit.endpoint,
        "direction": hit.direction,
        "evidence_role": "query_audit" if query_audit else hit.evidence_role,
        "evidence_type": "query_audit" if query_audit else hit.evidence_type,
        "provenance_status": provenance_status,
        "source_url": hit.source_url,
        "accession": hit.accession,
        "retrieved_at": hit.retrieved_at,
        "source_version": hit.source_version,
        "adapter_version": hit.adapter_version,
        "query_params": hit.query_params,
        "response_sha256": hit.response_sha256,
        "license": hit.license,
        "query_status": query_status,
        "lookup_field": hit.lookup_field,
        "lookup_value": hit.lookup_value,
        "match_type": hit.match_type,
        "claim_ceiling": hit.claim_ceiling,
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "baked_at": datetime.now(timezone.utc).isoformat(),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bake_manifest(
    *,
    output_path: Path,
    cfg: AppConfig,
    candidates: int,
    fetched: int,
    skipped_cached: int,
    wrote_rows: int,
    failures: int,
    records: list[MoleculeRecord],
    source_input_sha256: str = "",
) -> tuple[Path, str]:
    snapshot_sha256 = _sha256_file(output_path) if output_path.is_file() else ""
    freeze_path = REPO_ROOT / "configs" / "algorithm_freeze.json"
    manifest_path = output_path.with_suffix(output_path.suffix + ".manifest.json")
    query_entities = []
    for record in records:
        mol = Chem.MolFromSmiles(record.smiles)
        standardized_inchikey = Chem.MolToInchiKey(mol) if mol is not None else ""
        query_entities.append(
            {
                "molecule_id": record.molecule_id,
                "source_index": record.source_index,
                "original_inchikey": record.inchikey,
                "standardized_inchikey": standardized_inchikey,
                "standardized_smiles": record.smiles,
                "cas": record.cas or "",
            }
        )
    entity_bytes = json.dumps(
        query_entities,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload = {
        "schema_version": "molmind-evidence-bake-manifest-v3",
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "config_hash": cfg.config_hash,
        "source_input_sha256": source_input_sha256,
        "candidate_set_sha256": hashlib.sha256(entity_bytes).hexdigest(),
        "algorithm_freeze_sha256": _sha256_file(freeze_path) if freeze_path.is_file() else "",
        "snapshot_path": output_path.name,
        "snapshot_sha256": snapshot_sha256,
        "candidates": candidates,
        "fetched": fetched,
        "skipped_cached": skipped_cached,
        "wrote_rows": wrote_rows,
        "failures": failures,
        "query_entities": query_entities,
        "network_policy": "one_time_bake_then_snapshot_first",
        "negative_search_is_safety_evidence": False,
    }
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path, snapshot_sha256


def load_frozen_top10_records(
    entities_path: Path | None = None,
) -> list[MoleculeRecord]:
    """从冻结实体表构造精确查询记录，不重新执行候选选择。"""
    root = REPO_ROOT
    path = entities_path or (
        root / "data" / "evidence_snapshot" / "v2" / "top10_entities.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    records: list[MoleculeRecord] = []
    candidate_rows = sorted(
        payload.get("candidates") or [],
        key=lambda item: (int(item.get("baseline_rank") or 9999), str(item.get("molecule_id") or "")),
    )
    for row in candidate_rows:
        smiles = str(row["standardized_smiles"])
        mol = Chem.MolFromSmiles(smiles)
        desc = compute_descriptors(smiles)
        if mol is None or desc is None:
            raise ValueError(f"冻结候选结构无效: {row.get('molecule_id')}")
        records.append(
            MoleculeRecord(
                molecule_id=str(row["molecule_id"]),
                smiles=smiles,
                inchikey=str(row["original_inchikey"]),
                cas=str(row.get("cas") or "") or None,
                mw=float(desc["mw"]),
                logp=float(desc["logp"]),
                hbd=int(desc["hbd"]),
                hba=int(desc["hba"]),
                tpsa=float(desc["tpsa"]),
                rotatable_bonds=int(desc["rotatable_bonds"]),
                aromatic_rings=int(desc["aromatic_rings"]),
                fp_bits=morgan_fp(mol),
                source_index=int(row.get("source_index") or 0),
                source_molecule_id=str(row["molecule_id"]),
                original_smiles=str(row.get("original_smiles") or smiles),
                standardization_steps=("frozen_entity",),
            )
        )
    return records


def _standardized_inchikey(record: MoleculeRecord) -> str:
    mol = Chem.MolFromSmiles(record.smiles)
    return Chem.MolToInchiKey(mol) if mol is not None else ""


def _record_identity(record: MoleculeRecord) -> dict[str, Any]:
    """Build the exact identity vocabulary consumed by the Gateway resolver."""

    return {
        "molecule_id": record.molecule_id,
        "original_inchikey": record.inchikey,
        "standardized_inchikey": _standardized_inchikey(record),
        "cas": record.cas or "",
        "standardized_smiles": record.smiles,
        "original_smiles": record.original_smiles or "",
        "standardization_steps": list(record.standardization_steps),
    }


def _snapshot_providers_for(
    facade: EvidenceFacade,
    inchikey: str,
    cas: str | None,
    standardized_inchikey: str = "",
) -> set[str]:
    """Return primary providers already frozen for this exact identity."""

    candidate_keys = {
        str(value or "").strip().upper()
        for value in (inchikey, standardized_inchikey)
        if str(value or "").strip()
    }
    provider_markers = {
        "chembl": ("chembl_lipid_v",),
        "pubchem": ("pubchem_tox_v",),
        "epa_ctx": ("epa_ctx_candidate", "epa_ctx_identity", "epa_ctx_tox"),
    }
    covered: set[str] = set()
    for lookup_field, key in (
        ("inchikey", inchikey),
        ("inchikey", standardized_inchikey),
        ("cas", cas or ""),
    ):
        if not key:
            continue
        for row in facade._index.get(key, []):
            row_key = str(row.get("inchikey") or "").strip().upper()
            if lookup_field == "cas" and row_key and row_key not in candidate_keys:
                continue
            status = str(row.get("query_status") or "not_queried")
            if status not in {
                "hit",
                "exact_hit",
                "analogue_hit",
                "annotation_only",
                "verified_empty",
                "identity_review_required",
            }:
                continue
            explicit = str(row.get("provider_id") or "").strip()
            adapter = str(row.get("adapter_id") or "").lower()
            for provider, markers in provider_markers.items():
                if explicit == provider or any(marker in adapter for marker in markers):
                    covered.add(provider)
    return covered


def _gateway_cache_path(
    provider_config: Mapping[str, Any],
    override: Path | None,
) -> Path:
    if override is not None:
        return Path(override)
    cache_config = provider_config.get("cache")
    state_db = (
        cache_config.get("state_db")
        if isinstance(cache_config, Mapping)
        else None
    )
    configured = Path(str(state_db or "data/public/cache/evidence_query_state.sqlite"))
    return configured if configured.is_absolute() else REPO_ROOT / configured


def _bake_adapters(
    facade: EvidenceFacade,
    provider_configs: Mapping[str, Mapping[str, Any]],
    requested_providers: Sequence[str],
    overrides: Mapping[str, ProviderAdapter] | None,
) -> dict[str, ProviderAdapter]:
    adapters = dict(overrides or {})
    if any(provider not in adapters for provider in requested_providers):
        # Keep the HTTP adapter implementation shared with query_evidence.  The
        # bake path itself never calls EvidenceFacade._try_live; provider
        # scheduling, retries and circuits are owned by EvidenceRetriever.
        from plugins.molmind_core.tools.evidence_query import _default_adapters

        defaults = _default_adapters(facade, provider_configs)
        adapters = {**defaults, **adapters}
    return adapters


def select_bake_candidates(
    input_path: Path,
    cfg: AppConfig,
    *,
    top_m: int,
) -> list[MoleculeRecord]:
    from plugins.molmind_core.scientific.ingest import (
        feature_cache_path,
        load_feature_cache,
        save_feature_cache,
    )

    gold = load_goldset()
    facade = EvidenceFacade(cfg)

    cache_cfg = cfg.feature_cache or {}
    cache_enabled = bool(cache_cfg.get("enabled", True))
    cache_dir = Path(str(cache_cfg.get("directory") or ".molmind_cache/features"))
    if not cache_dir.is_absolute():
        cache_dir = REPO_ROOT / cache_dir
    schema_version = str(cache_cfg.get("schema_version") or "ingest-features-v1")
    cache_path = feature_cache_path(
        input_path, cache_dir=cache_dir, schema_version=schema_version
    )
    parsed = load_feature_cache(cache_path) if cache_enabled else None
    if parsed is None:
        with quiet_rdkit():
            parsed = parse_sdf_detailed(input_path)
        if cache_enabled:
            try:
                save_feature_cache(
                    cache_path,
                    parsed,
                    metadata={
                        "schema_version": schema_version,
                        "input": str(input_path),
                    },
                )
            except OSError:
                pass

    passed: list[MoleculeRecord] = []
    for record in parsed.records:
        if apply_hard_filters(record, cfg).passed:
            passed.append(record)

    scored = []
    for record in passed:
        ev = facade.query(
            inchikey=record.inchikey,
            cas=record.cas,
            smiles=record.smiles,
            allow_live=False,
        )
        scored.append((score_molecule(record, cfg, gold, ev), record))

    eligible = [(s, r) for s, r in scored if not s.gated_out]
    eligible.sort(key=lambda x: (-x[0].final_score, x[0].molecule_id))
    return [r for _, r in eligible[:top_m]]


def bake_evidence_for_records(
    records: list[MoleculeRecord],
    cfg: AppConfig,
    *,
    output_path: Path | None = None,
    skip_cached: bool = True,
    source_input_sha256: str = "",
    providers: Sequence[str] | None = None,
    query_types: Sequence[str] | None = None,
    provider_config_path: Path | None = None,
    cache_path: Path | None = None,
    provider_adapters: Mapping[str, ProviderAdapter] | None = None,
    snapshot_dir: Path | None = None,
) -> BakeStats:
    """Explicitly enrich ``records`` through provider-batched Gateway execution.

    Frozen snapshot rows remain the first cache layer.  Every remaining record
    is planned together so provider concurrency, rate limits, retry backoff and
    circuits stay provider-local.  Query audits are frozen for traceability but
    are never converted into task evidence or non-zero score/confidence.
    """

    facade = EvidenceFacade(cfg, snapshot_dir=snapshot_dir)
    out = output_path or (SNAPSHOT_DIR / "baked_evidence_v2.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)

    requested_providers = list(
        dict.fromkeys(
            str(provider)
            for provider in (
                _DEFAULT_BAKE_PROVIDERS if providers is None else providers
            )
            if str(provider)
        )
    )
    requested_types = (
        list(dict.fromkeys(str(value) for value in query_types if str(value)))
        if query_types is not None
        else None
    )

    skipped = 0
    pending_records: list[MoleculeRecord] = []
    identities: list[dict[str, Any]] = []
    identity_by_molecule: dict[str, dict[str, Any]] = {}
    covered_by_molecule: dict[str, set[str]] = {}
    standardized_keys: dict[str, str] = {}
    for record in records:
        identity = _record_identity(record)
        standardized = str(identity["standardized_inchikey"] or "")
        standardized_keys[record.molecule_id] = standardized
        identity_by_molecule[record.molecule_id] = identity
        covered = (
            _snapshot_providers_for(
                facade,
                record.inchikey,
                record.cas,
                standardized,
            )
            if skip_cached
            else set()
        )
        covered_by_molecule[record.molecule_id] = covered
        if requested_providers and set(requested_providers) <= covered:
            skipped += 1
            continue
        pending_records.append(record)
        identities.append(identity)

    rows: list[dict] = []
    fetched_ids: set[str] = set()
    failed_ids: set[str] = set()

    if pending_records:
        raw_provider_config = load_provider_config(provider_config_path)
        raw_providers = raw_provider_config.get("providers")
        provider_configs: dict[str, Mapping[str, Any]] = {
            str(provider_id): value
            for provider_id, value in (
                raw_providers.items() if isinstance(raw_providers, Mapping) else ()
            )
            if isinstance(value, Mapping)
        }
        cache = EvidenceQueryCache(
            _gateway_cache_path(raw_provider_config, cache_path),
            config=raw_provider_config,
        )
        try:
            retriever = EvidenceRetriever(
                cache,
                raw_provider_config,
                _bake_adapters(
                    facade,
                    provider_configs,
                    requested_providers,
                    provider_adapters,
                ),
            )
            has_partial_snapshot_coverage = any(
                covered_by_molecule.get(record.molecule_id, set())
                for record in pending_records
            )
            retrievals = []
            if has_partial_snapshot_coverage:
                # Plan only the provider/entity gaps. This preserves the
                # snapshot-first contract without issuing a duplicate request
                # to a provider already frozen for that molecule.
                for provider in requested_providers:
                    provider_identities = [
                        identity_by_molecule[record.molecule_id]
                        for record in pending_records
                        if provider
                        not in covered_by_molecule.get(record.molecule_id, set())
                    ]
                    if not provider_identities:
                        continue
                    retrievals.append(
                        retriever.query(
                            provider_identities,
                            providers=[provider],
                            query_types=requested_types,
                            allow_live=True,
                            force_refresh=not skip_cached,
                        )
                    )
            else:
                retrievals.append(
                    retriever.query(
                        identities,
                        providers=requested_providers,
                        query_types=requested_types,
                        allow_live=True,
                        # skip_cached=False is the historical explicit refresh
                        # mode. Gateway backoff still wins over force_refresh.
                        force_refresh=not skip_cached,
                    )
                )
        finally:
            cache.close()

        fetched_ids = {
            str(event.get("molecule_id") or "")
            for retrieval in retrievals
            for event in retrieval.events
            if event.get("type") == "remote_start" and event.get("molecule_id")
        }
        for record in pending_records:
            molecule_hits = [
                *(
                    hit
                    for retrieval in retrievals
                    for hit in (
                        retrieval.hits_by_molecule.get(record.molecule_id) or []
                    )
                ),
                *(
                    hit
                    for retrieval in retrievals
                    for hit in (
                        retrieval.audits_by_molecule.get(record.molecule_id) or []
                    )
                ),
            ]
            if any(str(hit.query_status) in _FAILED_QUERY_STATUSES for hit in molecule_hits):
                failed_ids.add(record.molecule_id)
            standardized = standardized_keys.get(record.molecule_id, "")
            snapshot_key = record.inchikey or standardized
            rows.extend(
                _hit_to_row(
                    hit,
                    inchikey=snapshot_key,
                    cas=record.cas,
                    molecule_id=record.molecule_id,
                    standardized_inchikey=standardized,
                    original_inchikey=record.inchikey,
                )
                for hit in molecule_hits
            )

    wrote = 0
    if rows:
        with out.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                wrote += 1

    # force 重拉后压缩，使同 InChIKey 以最新行为准（覆盖旧快照）
    if not skip_cached and out.is_file():
        from plugins.molmind_core.scientific.evidence_facade.snapshot_compact import compact_snapshot_jsonl

        compact_snapshot_jsonl(out, backup=True)

    manifest_path, snapshot_sha256 = _write_bake_manifest(
        output_path=out,
        cfg=cfg,
        candidates=len(records),
        fetched=len(fetched_ids),
        skipped_cached=skipped,
        wrote_rows=wrote,
        failures=len(failed_ids),
        records=records,
        source_input_sha256=source_input_sha256,
    )
    return BakeStats(
        candidates=len(records),
        fetched=len(fetched_ids),
        skipped_cached=skipped,
        wrote_rows=wrote,
        failures=len(failed_ids),
        output_path=str(out),
        manifest_path=str(manifest_path),
        snapshot_sha256=snapshot_sha256,
    )


def bake_from_sdf(
    input_path: Path,
    *,
    top_m: int | None = None,
    output_path: Path | None = None,
    skip_cached: bool = True,
) -> BakeStats:
    cfg = load_config(allow_live=True)
    m = top_m or int(cfg.evidence.get("deep_query_top_m", 40))
    m = max(m, int(cfg.evidence.get("bake_top_m", m)))
    records = select_bake_candidates(input_path, cfg, top_m=m)
    return bake_evidence_for_records(
        records,
        cfg,
        output_path=output_path,
        skip_cached=skip_cached,
        source_input_sha256=_sha256_file(input_path),
    )


def bake_frozen_top10(
    *,
    output_path: Path | None = None,
    skip_cached: bool = True,
    entities_path: Path | None = None,
) -> BakeStats:
    cfg = load_config(allow_live=True)
    root = REPO_ROOT
    resolved_entities_path = entities_path or (
        root / "data" / "evidence_snapshot" / "v2" / "top10_entities.json"
    )
    entity_payload = json.loads(resolved_entities_path.read_text(encoding="utf-8"))
    return bake_evidence_for_records(
        load_frozen_top10_records(resolved_entities_path),
        cfg,
        output_path=output_path,
        skip_cached=skip_cached,
        source_input_sha256=str(entity_payload.get("source_input_sha256") or ""),
    )


def bake_submission_evidence(
    input_path: Path,
    *,
    top_m: int | None = None,
    output_path: Path | None = None,
    skip_cached: bool = True,
) -> BakeStats:
    """一次覆盖冻结 Top 10 和日常 auto 短名单窗口。"""
    cfg = load_config(allow_live=True)
    m = top_m or int(cfg.evidence.get("bake_top_m", 80))
    frozen = load_frozen_top10_records()
    selected = select_bake_candidates(input_path, cfg, top_m=m)
    frozen_cas = {record.cas for record in frozen if record.cas}
    frozen_ids = {record.molecule_id for record in frozen}
    combined = [
        *frozen,
        *[
            record
            for record in selected
            if record.molecule_id not in frozen_ids and record.cas not in frozen_cas
        ],
    ]
    return bake_evidence_for_records(
        combined,
        cfg,
        output_path=output_path,
        skip_cached=skip_cached,
        source_input_sha256=_sha256_file(input_path),
    )


def promote_evidence_cache(
    *,
    cache_path: Path,
    output_path: Path,
    provider_config_path: Path | None = None,
    dry_run: bool = False,
    previous_top_n: Sequence[str] | None = None,
) -> PromoteStats:
    """Validate and atomically promote live cache bundles to a frozen snapshot.

    This is deliberately separate from ``bake_evidence_for_records``: query
    results are never ranking input until this explicit promotion succeeds.
    """

    raw_config = load_provider_config(provider_config_path)
    cache = EvidenceQueryCache(Path(cache_path), config=raw_config)
    staging = Path(output_path).with_name(Path(output_path).name + ".staging")
    manifest = Path(output_path).with_suffix(Path(output_path).suffix + ".manifest.json")
    rows: list[dict[str, Any]] = []
    rejected = 0
    contracts: set[str] = set()
    try:
        source_rows = cache.db.execute(
            "SELECT * FROM source_query ORDER BY source_id, entity_key, endpoint, "
            "query_contract_hash"
        ).fetchall()
        for state in source_rows:
            payload = cache.load_payload(
                source_id=state["source_id"],
                entity_key=state["entity_key"],
                endpoint=state["endpoint"],
                query_contract_hash=str(state["query_contract_hash"] or ""),
            )
            if not isinstance(payload, list):
                payload = payload.get("hits") if isinstance(payload, Mapping) else None
            if not isinstance(payload, list):
                continue
            contract_hash = str(state["query_contract_hash"] or "")
            if contract_hash:
                contracts.add(contract_hash)
            for raw in payload:
                if not isinstance(raw, Mapping):
                    rejected += 1
                    continue
                raw_status = str(raw.get("raw_status") or raw.get("query_status") or "")
                status = canonical_status(raw_status)
                if status not in CANONICAL_STATUSES or status in _FAILED_QUERY_STATUSES:
                    rejected += 1
                    continue
                lookup_field = str(raw.get("lookup_field") or state["lookup_field"] or "")
                lookup_value = str(raw.get("lookup_value") or state["lookup_value"] or "")
                original_key = str(raw.get("original_inchikey") or raw.get("inchikey") or "")
                standardized_key = str(raw.get("standardized_inchikey") or "")
                cas_value = raw.get("cas") or ""
                smiles_value = str(raw.get("standardized_smiles") or "")
                if lookup_field in {"original_inchikey", "inchikey"} and not original_key:
                    original_key = lookup_value
                elif lookup_field == "standardized_inchikey" and not standardized_key:
                    standardized_key = lookup_value
                elif lookup_field == "cas" and not cas_value:
                    cas_value = lookup_value
                elif lookup_field in {"standardized_smiles", "smiles"} and not smiles_value:
                    smiles_value = lookup_value
                identity = resolve_identity(
                    molecule_id=str(raw.get("molecule_id") or state["entity_key"]),
                    original_inchikey=original_key,
                    standardized_inchikey=standardized_key,
                    cas=cas_value,
                    smiles=smiles_value,
                )
                if identity.requires_review or not identity.is_resolved:
                    rejected += 1
                    continue
                row = dict(raw)
                row["query_status"] = status
                row["raw_status"] = raw_status
                row["query_contract_hash"] = contract_hash
                row["provider_id"] = str(row.get("provider_id") or state["source_id"])
                row["adapter_version"] = str(
                    row.get("adapter_version") or state["adapter_version"] or ""
                )
                role = str(row.get("evidence_role") or "")
                if role in {"annotation_only", "mechanism_support"}:
                    row["score"] = 0.0
                row = json_safe(row)
                rows.append(row)
    finally:
        cache.close()

    dedup: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("molecule_id") or ""),
            str(row.get("provider_id") or ""),
            str(row.get("query_type") or ""),
            str(row.get("evidence_id") or ""),
        )
        dedup[key] = row
    rows = [
        dedup[key]
        for key in sorted(dedup, key=lambda item: item)
    ]
    diff = {
        "previous_top_n": list(previous_top_n or ()),
        "promoted_molecule_ids": sorted(
            {str(row.get("molecule_id") or "") for row in rows if row.get("molecule_id")}
        ),
    }
    snapshot_bytes = (
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    ).encode("utf-8")
    snapshot_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
    manifest_payload = {
        "schema_version": "molmind-evidence-promote-manifest-v1",
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_cache": str(cache_path),
        "provider_config_path": str(provider_config_path or REPO_ROOT / "configs/evidence_providers.yaml"),
        "query_contract_hashes": sorted(contracts),
        "rows": len(rows),
        "rejected": rejected,
        "snapshot_path": Path(output_path).name,
        "snapshot_sha256": snapshot_sha256,
        "atomic_publish": True,
        "offline_replay_required": True,
        "diff": diff,
    }
    if not dry_run:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        fd, staging_name = tempfile.mkstemp(
            prefix=Path(output_path).name + ".", suffix=".tmp",
            dir=str(Path(output_path).parent),
        )
        os.close(fd)
        staging_path = Path(staging_name)
        try:
            staging_path.write_bytes(snapshot_bytes)
            if hashlib.sha256(staging_path.read_bytes()).hexdigest() != snapshot_sha256:
                raise ValueError("staging snapshot hash validation failed")
            staging_path.replace(output_path)
            manifest_tmp = manifest.with_suffix(manifest.suffix + ".tmp")
            manifest_tmp.write_text(
                json.dumps(manifest_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            manifest_tmp.replace(manifest)
        except Exception:
            staging_path.unlink(missing_ok=True)
            raise
    return PromoteStats(
        rows=len(rows),
        rejected=rejected,
        output_path=str(output_path),
        manifest_path=str(manifest),
        snapshot_sha256=snapshot_sha256,
        dry_run=dry_run,
        diff=diff,
    )
