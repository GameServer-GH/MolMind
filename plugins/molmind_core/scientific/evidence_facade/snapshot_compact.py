"""证据快照 JSONL：按 InChIKey+adapter+query_type 保留最后一条（覆盖旧缓存）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from plugins.molmind_core.scientific.pipeline.config_loader import SNAPSHOT_DIR


@dataclass
class CompactStats:
    input_rows: int
    output_rows: int
    dropped_duplicates: int
    path: str


def _row_key(row: dict) -> str:
    inchikey = str(row.get("inchikey") or "").strip()
    cas = str(row.get("cas") or "").strip()
    adapter = str(row.get("adapter_id") or "").strip()
    qtype = str(row.get("query_type") or "").strip()
    identity = inchikey or cas or ""
    return f"{identity}|{adapter}|{qtype}"


def compact_snapshot_jsonl(
    path: Path,
    *,
    backup: bool = True,
) -> CompactStats:
    """原地压缩：同 key 保留文件中最后出现的一行（新 live 覆盖旧快照）。"""
    if not path.is_file():
        return CompactStats(0, 0, 0, str(path))

    kept: dict[str, dict] = {}
    order: list[str] = []
    input_rows = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            key = _row_key(row)
            if not key.startswith("|") and key != "||":
                input_rows += 1
                if key not in kept:
                    order.append(key)
                kept[key] = row

    output_rows = len(order)
    dropped = max(0, input_rows - output_rows)

    if backup:
        bak = path.with_suffix(path.suffix + ".bak")
        bak.write_bytes(path.read_bytes())

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for key in order:
            fh.write(json.dumps(kept[key], ensure_ascii=False) + "\n")
    tmp.replace(path)
    return CompactStats(
        input_rows=input_rows,
        output_rows=output_rows,
        dropped_duplicates=dropped,
        path=str(path),
    )


def compact_all_snapshots(
    snapshot_dir: Path | None = None,
    *,
    backup: bool = True,
) -> list[CompactStats]:
    root = snapshot_dir or SNAPSHOT_DIR
    stats: list[CompactStats] = []
    if not root.is_dir():
        return stats
    for path in sorted(root.glob("*.jsonl")):
        stats.append(compact_snapshot_jsonl(path, backup=backup))
    return stats
