"""安全的化学特征缓存：gzip JSON，不使用 pickle，不缓存最终分数。"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from rdkit import DataStructs, rdBase

from packages.models import MoleculeRecord, ParseIssue
from plugins.molmind_core.scientific.ingest.parser import ParseResult


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def feature_cache_key(
    input_path: Path,
    *,
    schema_version: str,
    content_sha256: str | None = None,
) -> str:
    digest = content_sha256 or sha256_file(input_path)
    payload = f"{digest}|rdkit={rdBase.rdkitVersion}|schema={schema_version}"
    return hashlib.sha256(payload.encode()).hexdigest()


def feature_cache_path(
    input_path: Path,
    *,
    cache_dir: Path,
    schema_version: str,
    content_sha256: str | None = None,
) -> Path:
    return cache_dir / (
        f"{feature_cache_key(input_path, schema_version=schema_version, content_sha256=content_sha256)}.json.gz"
    )


def _fp_from_bits(n_bits: int, on_bits: list[int]):
    fp = DataStructs.ExplicitBitVect(n_bits)
    for bit in on_bits:
        if 0 <= int(bit) < n_bits:
            fp.SetBit(int(bit))
    return fp


def load_feature_cache(path: Path) -> ParseResult | None:
    if not path.is_file():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            raw = json.load(handle)
        records: list[MoleculeRecord] = []
        for row in raw.get("records") or []:
            item = dict(row)
            n_bits = int(item.pop("fp_n_bits"))
            on_bits = [int(v) for v in item.pop("fp_on_bits")]
            item["standardization_steps"] = tuple(item.get("standardization_steps") or [])
            item["fp_bits"] = _fp_from_bits(n_bits, on_bits)
            records.append(MoleculeRecord(**item))
        issues = [ParseIssue(**row) for row in raw.get("issues") or []]
        return ParseResult(
            records=records,
            raw_count=int(raw["raw_count"]),
            skipped=int(raw["skipped"]),
            inchikey_missing=int(raw["inchikey_missing"]),
            issues=issues,
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def save_feature_cache(path: Path, result: ParseResult, *, metadata: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    for record in result.records:
        row = asdict(record)
        row.pop("fp_bits", None)
        row["fp_n_bits"] = int(record.fp_bits.GetNumBits())
        row["fp_on_bits"] = [int(bit) for bit in record.fp_bits.GetOnBits()]
        records.append(row)
    payload = {
        "metadata": metadata,
        "raw_count": result.raw_count,
        "skipped": result.skipped,
        "inchikey_missing": result.inchikey_missing,
        "records": records,
        "issues": [asdict(issue) for issue in result.issues],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)
