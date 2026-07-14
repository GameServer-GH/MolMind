"""把 ChEMBL / PubChem 证据烘焙为本地 JSONL 快照。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from packages.goldset import load_goldset
from packages.models import EvidenceHit, MoleculeRecord
from services.evidence_facade.facade import EvidenceFacade
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
        "baked_at": datetime.now(timezone.utc).isoformat(),
    }


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
    gold = load_goldset()
    facade = EvidenceFacade(cfg)

    with quiet_rdkit():
        parsed = parse_sdf_detailed(input_path)
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
) -> BakeStats:
    facade = EvidenceFacade(cfg)
    original_mode = cfg.mode
    cfg.mode = "online"
    facade._circuit_open = False

    out = output_path or (SNAPSHOT_DIR / "baked_chembl_pubchem.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)

    wrote = 0
    fetched = 0
    skipped = 0
    failures = 0
    rows: list[dict] = []

    for record in records:
        if not record.inchikey:
            failures += 1
            continue
        if skip_cached and _has_snapshot_for(facade, record.inchikey, record.cas):
            skipped += 1
            continue
        try:
            live = facade._try_live(
                inchikey=record.inchikey,
                cas=record.cas,
                smiles=record.smiles,
            )
            fetched += 1
            for hit in live:
                rows.append(_hit_to_row(hit, inchikey=record.inchikey, cas=record.cas))
            if not live:
                rows.append(
                    {
                        "inchikey": record.inchikey,
                        "cas": record.cas or "",
                        "adapter_id": "bake_miss_v1",
                        "query_type": "novelty",
                        "score": 0.5,
                        "confidence": 0.1,
                        "evidence_id": f"bake_miss:{record.inchikey}",
                        "payload": {"note": "live queried, no chembl/pubchem hit"},
                        "baked_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
        except Exception:
            failures += 1

    if rows:
        with out.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                wrote += 1

    cfg.mode = original_mode

    # force 重拉后压缩，使同 InChIKey 以最新行为准（覆盖旧快照）
    if not skip_cached and out.is_file():
        from services.evidence_facade.snapshot_compact import compact_snapshot_jsonl

        compact_snapshot_jsonl(out, backup=True)

    return BakeStats(
        candidates=len(records),
        fetched=fetched,
        skipped_cached=skipped,
        wrote_rows=wrote,
        failures=failures,
        output_path=str(out),
    )


def bake_from_sdf(
    input_path: Path,
    *,
    top_m: int | None = None,
    output_path: Path | None = None,
    skip_cached: bool = True,
) -> BakeStats:
    cfg = load_config(mode="online")
    m = top_m or int(cfg.evidence.get("deep_query_top_m", 40))
    m = max(m, int(cfg.evidence.get("bake_top_m", m)))
    records = select_bake_candidates(input_path, cfg, top_m=m)
    return bake_evidence_for_records(
        records,
        cfg,
        output_path=output_path,
        skip_cached=skip_cached,
    )
