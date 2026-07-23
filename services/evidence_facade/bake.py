"""把 ChEMBL / PubChem 证据烘焙为本地 JSONL 快照。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rdkit import Chem

from packages.chem_core import compute_descriptors, morgan_fp
from packages.goldset import load_goldset
from packages.models import EvidenceHit, MoleculeRecord
from services.evidence_facade.facade import EVIDENCE_SCHEMA_VERSION, EvidenceFacade
from services.hard_filter import apply_hard_filters
from services.ingest import parse_sdf_detailed, quiet_rdkit
from services.pipeline.config_loader import SNAPSHOT_DIR, AppConfig, load_config
from services.ranker import score_molecule


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


def _hit_to_row(hit: EvidenceHit, *, inchikey: str, cas: str | None) -> dict:
    return {
        "inchikey": inchikey,
        "cas": cas or "",
        "adapter_id": hit.adapter_id,
        "query_type": hit.query_type,
        "score": hit.score,
        "confidence": hit.confidence,
        "evidence_id": hit.evidence_id,
        "payload": hit.payload,
        "endpoint": hit.endpoint,
        "direction": hit.direction,
        "evidence_role": hit.evidence_role,
        "provenance_status": hit.provenance_status,
        "source_url": hit.source_url,
        "retrieved_at": hit.retrieved_at,
        "adapter_version": hit.adapter_version,
        "query_params": hit.query_params,
        "response_sha256": hit.response_sha256,
        "license": hit.license,
        "query_status": hit.query_status,
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "baked_at": datetime.now(timezone.utc).isoformat(),
    }


def _miss_row(*, inchikey: str, cas: str | None) -> dict:
    """记录查询状态；未检出永远不是低毒、新颖性或药效证据。"""
    return {
        "inchikey": inchikey,
        "cas": cas or "",
        "adapter_id": "bake_miss_v1",
        "query_type": "query_audit",
        "score": 0.0,
        "confidence": 0.0,
        "evidence_id": f"bake_miss:{inchikey}",
        "payload": {"note": "live queried; no relevant ChEMBL/PubChem record"},
        "endpoint": "query_status",
        "direction": "unknown",
        "evidence_role": "query_audit",
        "provenance_status": "no_relevant_record",
        "query_status": "verified_empty",
        "source_url": "",
        "retrieved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "adapter_version": "bake_miss_v2",
        "query_params": {},
        "response_sha256": "",
        "license": "",
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "baked_at": datetime.now(timezone.utc).isoformat(),
    }


def _error_row(
    *,
    inchikey: str,
    cas: str | None,
    query_status: str = "adapter_error",
) -> dict:
    """网络或适配器失败必须可重试，不能伪装成确认未检出。"""
    return {
        "inchikey": inchikey,
        "cas": cas or "",
        "adapter_id": "bake_error_v2",
        "query_type": "query_audit",
        "score": 0.0,
        "confidence": 0.0,
        "evidence_id": f"bake_error:{inchikey}",
        "payload": {"note": "live query failed; retry required"},
        "endpoint": "query_status",
        "direction": "unknown",
        "evidence_role": "query_audit",
        "provenance_status": "query_failed",
        "query_status": query_status,
        "source_url": "",
        "retrieved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "adapter_version": "bake_error_v2",
        "query_params": {},
        "response_sha256": "",
        "license": "",
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
    freeze_path = Path(__file__).resolve().parents[2] / "configs" / "algorithm_freeze.json"
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
    root = Path(__file__).resolve().parents[2]
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


def _has_snapshot_for(facade: EvidenceFacade, inchikey: str, cas: str | None) -> bool:
    if inchikey and inchikey in facade._index:
        return True
    if cas and cas in facade._index:
        return True
    return False


def select_bake_candidates(
    input_path: Path,
    cfg: AppConfig,
    *,
    top_m: int,
) -> list[MoleculeRecord]:
    from services.ingest import (
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
        cache_dir = Path(__file__).resolve().parents[2] / cache_dir
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


def _apply_bake_network_policy(cfg: AppConfig, *, candidate_count: int) -> dict[str, object]:
    """Bake is one-time offline prep; do not inherit interactive SLA timeouts/circuits.

    ChEMBL molecule lookups commonly exceed the interactive ``http_timeout_sec=4``
    budget. Raising timeout and fail threshold here does not change scoring weights.

    ``AppConfig.evidence`` returns a copy, so bake must mutate ``cfg.raw['evidence']``.
    """
    evidence = cfg.raw.setdefault("evidence", {})
    previous = {
        "http_timeout_sec": evidence.get("http_timeout_sec"),
        "circuit_fail_threshold": evidence.get("circuit_fail_threshold"),
    }
    evidence["http_timeout_sec"] = float(evidence.get("bake_http_timeout_sec", 60.0))
    default_threshold = max(50, candidate_count * 2)
    evidence["circuit_fail_threshold"] = int(
        evidence.get("bake_circuit_fail_threshold", default_threshold)
    )
    return previous


def bake_evidence_for_records(
    records: list[MoleculeRecord],
    cfg: AppConfig,
    *,
    output_path: Path | None = None,
    skip_cached: bool = True,
    source_input_sha256: str = "",
) -> BakeStats:
    evidence = cfg.raw.setdefault("evidence", {})
    previous_network = {
        "allow_live": evidence.get("allow_live"),
        "http_timeout_sec": evidence.get("http_timeout_sec"),
        "circuit_fail_threshold": evidence.get("circuit_fail_threshold"),
    }
    previous_network.update(_apply_bake_network_policy(cfg, candidate_count=len(records)))
    evidence["allow_live"] = True
    facade = EvidenceFacade(cfg)
    facade._circuit_open = False
    facade._live_failures = 0

    out = output_path or (SNAPSHOT_DIR / "baked_evidence_v2.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)

    wrote = 0
    fetched = 0
    skipped = 0
    failures = 0
    rows: list[dict] = []

    try:
        for record in records:
            if not record.inchikey:
                failures += 1
                continue
            if skip_cached and _has_snapshot_for(facade, record.inchikey, record.cas):
                skipped += 1
                continue
            # One candidate timeout must not permanently open the bake circuit.
            facade._circuit_open = False
            try:
                failures_before = facade._live_failures
                live = facade._try_live(
                    inchikey=record.inchikey,
                    cas=record.cas,
                    smiles=record.smiles,
                )
                failure_statuses = {"timeout", "rate_limited", "adapter_error", "not_queried"}
                query_failed = any(
                    hit.provenance_status == "query_failed"
                    or hit.query_status in failure_statuses
                    for hit in live
                )
                if facade._live_failures > failures_before or query_failed:
                    failures += 1
                    audit_hits = [hit for hit in live if hit.query_type == "query_audit"]
                    if audit_hits:
                        rows.extend(
                            _hit_to_row(hit, inchikey=record.inchikey, cas=record.cas)
                            for hit in audit_hits
                        )
                    else:
                        rows.append(_error_row(inchikey=record.inchikey, cas=record.cas))
                    continue
                fetched += 1
                for hit in live:
                    rows.append(_hit_to_row(hit, inchikey=record.inchikey, cas=record.cas))
                if not live:
                    rows.append(_miss_row(inchikey=record.inchikey, cas=record.cas))
            except Exception:
                failures += 1

        if rows:
            with out.open("a", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    wrote += 1

        # force 重拉后压缩，使同 InChIKey 以最新行为准（覆盖旧快照）
        if not skip_cached and out.is_file():
            from services.evidence_facade.snapshot_compact import compact_snapshot_jsonl

            compact_snapshot_jsonl(out, backup=True)

        manifest_path, snapshot_sha256 = _write_bake_manifest(
            output_path=out,
            cfg=cfg,
            candidates=len(records),
            fetched=fetched,
            skipped_cached=skipped,
            wrote_rows=wrote,
            failures=failures,
            records=records,
            source_input_sha256=source_input_sha256,
        )
        return BakeStats(
            candidates=len(records),
            fetched=fetched,
            skipped_cached=skipped,
            wrote_rows=wrote,
            failures=failures,
            output_path=str(out),
            manifest_path=str(manifest_path),
            snapshot_sha256=snapshot_sha256,
        )
    finally:
        evidence = cfg.raw.setdefault("evidence", {})
        for key, value in previous_network.items():
            if value is None:
                evidence.pop(str(key), None)
            else:
                evidence[str(key)] = value


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
    root = Path(__file__).resolve().parents[2]
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
