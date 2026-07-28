"""DILIrank exact-identity gate (risk only; never raises safety scores).

Coverage is sparse relative to a screening library, so this module never
feeds ``dili_table_v1`` ranking weights. Exact Most-DILI matches may hard
exclude; Less/Ambiguous/No-DILI stay audit/annotation only.
"""

from __future__ import annotations

from plugins.molmind_core.scientific.paths import REPO_ROOT
import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = REPO_ROOT
DEFAULT_IDENTITY_PATH = ROOT / "data/reference/dilirank_identity_mapped.jsonl"
DEFAULT_OFFICIAL_CSV = ROOT / "data/public/processed/fda_dilirank_2/records.csv"
DEFAULT_REFERENCE_CSV = ROOT / "data/reference/dilirank.csv"
DEFAULT_EPA_MAPPING = (
    ROOT / "data/public/processed/epa_ctx/bulk_mapping_all_enriched.jsonl"
)
DEFAULT_PROCESSED_IDENTITY_PATH = (
    ROOT / "data/public/processed/fda_dilirank_2/identity_mapped.jsonl"
)

_SALT_TOKENS = (
    "sulfate",
    "sulphate",
    "hydrochloride",
    "hydrobromide",
    "sodium",
    "potassium",
    "calcium",
    "magnesium",
    "acetate",
    "mesylate",
    "maleate",
    "chloride",
    "bromide",
    "phosphate",
    "tartrate",
    "citrate",
    "fumarate",
)


def normalize_drug_name(value: str | None) -> str:
    text = str(value or "").lower().strip()
    text = re.sub(r"\s*\(.*?\)\s*", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def name_variants(value: str | None) -> set[str]:
    base = normalize_drug_name(value)
    if not base:
        return set()
    out = {base}
    tokens = base.split()
    stripped = [t for t in tokens if t not in _SALT_TOKENS]
    if stripped:
        out.add(" ".join(stripped))
    return {item for item in out if item}


def normalize_concern(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    raw = raw.replace("vdili-", "dili-").replace("dili-", "dili-")
    if "most" in raw:
        return "most"
    if "less" in raw:
        return "less"
    if "no" in raw and "dili" in raw:
        return "no"
    if "ambiguous" in raw:
        return "ambiguous"
    if "curated-most" in raw or raw == "curated-most":
        return "most"
    return "unknown"


def concern_action(concern: str, *, hard_exclude_most: bool) -> str:
    if concern == "most" and hard_exclude_most:
        return "hard_exclude"
    if concern in {"most", "less", "ambiguous", "no", "unknown"}:
        return "annotate_only"
    return "annotate_only"


@dataclass
class DiliIdentityRecord:
    ltkb_id: str
    compound_name: str
    concern: str
    concern_raw: str
    inchikey: str
    cas: str = ""
    molecule_id: str = ""
    match_basis: str = ""
    source: str = ""


@dataclass
class DiliRankIndex:
    by_inchikey: dict[str, list[DiliIdentityRecord]] = field(default_factory=dict)
    by_cas: dict[str, list[DiliIdentityRecord]] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return sum(len(rows) for rows in self.by_inchikey.values()) + sum(
            len(rows) for rows in self.by_cas.values()
        )

    def lookup(
        self,
        *,
        inchikey: str | None,
        cas: str | None = None,
        extra_inchikeys: Iterable[str] | None = None,
    ) -> DiliIdentityRecord | None:
        keys: list[str] = []
        for key in (inchikey, *(extra_inchikeys or ())):
            value = str(key or "").strip().upper()
            if value and value not in keys:
                keys.append(value)
        for key in keys:
            rows = self.by_inchikey.get(key) or []
            if rows:
                return _prefer_most(rows)
        cas_key = str(cas or "").strip()
        if cas_key:
            rows = self.by_cas.get(cas_key) or []
            if rows:
                return _prefer_most(rows)
        return None


def _prefer_most(rows: list[DiliIdentityRecord]) -> DiliIdentityRecord:
    for row in rows:
        if row.concern == "most":
            return row
    return rows[0]


def _add_record(index: DiliRankIndex, record: DiliIdentityRecord) -> None:
    ik = record.inchikey.strip().upper()
    if ik:
        bucket = index.by_inchikey.setdefault(ik, [])
        if not any(
            existing.ltkb_id == record.ltkb_id and existing.concern == record.concern
            for existing in bucket
        ):
            bucket.append(record)
    cas = record.cas.strip()
    if cas:
        bucket = index.by_cas.setdefault(cas, [])
        if not any(
            existing.ltkb_id == record.ltkb_id and existing.concern == record.concern
            for existing in bucket
        ):
            bucket.append(record)


def load_identity_jsonl(path: Path) -> DiliRankIndex:
    index = DiliRankIndex()
    if not path.is_file():
        return index
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        record = DiliIdentityRecord(
            ltkb_id=str(row.get("ltkb_id") or ""),
            compound_name=str(row.get("compound_name") or ""),
            concern=normalize_concern(row.get("concern") or row.get("concern_raw")),
            concern_raw=str(row.get("concern_raw") or row.get("concern") or ""),
            inchikey=str(row.get("inchikey") or ""),
            cas=str(row.get("cas") or ""),
            molecule_id=str(row.get("molecule_id") or ""),
            match_basis=str(row.get("match_basis") or ""),
            source=str(row.get("source") or "identity_mapped"),
        )
        if record.inchikey or record.cas:
            _add_record(index, record)
    return index


def load_reference_csv(path: Path) -> DiliRankIndex:
    index = DiliRankIndex()
    if not path.is_file():
        return index
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            record = DiliIdentityRecord(
                ltkb_id=str(row.get("ltkb_id") or ""),
                compound_name=str(row.get("name") or ""),
                concern=normalize_concern(row.get("concern")),
                concern_raw=str(row.get("concern") or ""),
                inchikey=str(row.get("inchikey") or ""),
                cas="",
                match_basis="reference_curated_inchikey",
                source=str(row.get("source") or "dilirank_reference"),
            )
            if record.inchikey:
                _add_record(index, record)
    return index


def merge_indices(*indices: DiliRankIndex) -> DiliRankIndex:
    merged = DiliRankIndex()
    for index in indices:
        for rows in index.by_inchikey.values():
            for row in rows:
                _add_record(merged, row)
        for rows in index.by_cas.values():
            for row in rows:
                _add_record(merged, row)
    return merged


def load_dilirank_index_from_config(cfg: Mapping[str, Any] | None) -> DiliRankIndex:
    gate = dict(cfg or {})
    paths = [
        Path(str(item))
        for item in (
            gate.get("identity_paths")
            or [str(DEFAULT_IDENTITY_PATH), str(DEFAULT_REFERENCE_CSV)]
        )
    ]
    indices: list[DiliRankIndex] = []
    for path in paths:
        resolved = path if path.is_absolute() else ROOT / path
        if resolved.suffix.lower() == ".jsonl":
            indices.append(load_identity_jsonl(resolved))
        elif resolved.suffix.lower() == ".csv":
            indices.append(load_reference_csv(resolved))
    return merge_indices(*indices) if indices else DiliRankIndex()


def build_identity_rows(
    *,
    official_csv: Path = DEFAULT_OFFICIAL_CSV,
    reference_csv: Path = DEFAULT_REFERENCE_CSV,
    epa_mapping: Path = DEFAULT_EPA_MAPPING,
) -> list[dict[str, Any]]:
    """Offline identity alignment: curated InChIKeys + EPA preferred_name exact."""
    by_name: dict[str, list[dict[str, Any]]] = {}
    if epa_mapping.is_file():
        for line in epa_mapping.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if str(row.get("mapping_status") or "") != "exact_identifier_match":
                continue
            for variant in name_variants(row.get("preferred_name")):
                by_name.setdefault(variant, []).append(row)

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def push(payload: dict[str, Any]) -> None:
        key = (
            str(payload.get("ltkb_id") or ""),
            str(payload.get("inchikey") or "").upper(),
            str(payload.get("concern") or ""),
        )
        if key in seen:
            return
        seen.add(key)
        out.append(payload)

    if reference_csv.is_file():
        with reference_csv.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                ik = str(row.get("inchikey") or "").strip()
                if not ik:
                    continue
                push(
                    {
                        "ltkb_id": str(row.get("ltkb_id") or ""),
                        "compound_name": str(row.get("name") or ""),
                        "concern": normalize_concern(row.get("concern")),
                        "concern_raw": str(row.get("concern") or ""),
                        "inchikey": ik,
                        "cas": "",
                        "molecule_id": "",
                        "match_basis": "reference_curated_inchikey",
                        "source": str(row.get("source") or "dilirank_reference"),
                    }
                )

    if official_csv.is_file():
        lines = official_csv.read_text(encoding="utf-8").splitlines()
        reader = csv.DictReader(io.StringIO("\n".join(lines[1:])))
        for row in reader:
            name = str(row.get("CompoundName") or "")
            concern_raw = str(row.get("vDILI-Concern") or "")
            concern = normalize_concern(concern_raw)
            ltkb = str(row.get("LTKBID") or "")
            matched = None
            for variant in name_variants(name):
                candidates = by_name.get(variant) or []
                if candidates:
                    matched = candidates[0]
                    break
            if matched is None:
                continue
            push(
                {
                    "ltkb_id": ltkb,
                    "compound_name": name,
                    "concern": concern,
                    "concern_raw": concern_raw,
                    "inchikey": str(
                        matched.get("original_inchikey")
                        or matched.get("standardized_inchikey")
                        or ""
                    ),
                    "cas": str(matched.get("cas") or matched.get("casrn") or ""),
                    "molecule_id": str(matched.get("id") or matched.get("molecule_id") or ""),
                    "match_basis": "epa_preferred_name_exact",
                    "source": "fda_dilirank_2+epa_ctx_mapping",
                    "dtxsid": str(matched.get("dtxsid") or ""),
                    "preferred_name": str(matched.get("preferred_name") or ""),
                }
            )
    return out


def write_identity_jsonl(
    output_path: Path,
    *,
    official_csv: Path = DEFAULT_OFFICIAL_CSV,
    reference_csv: Path = DEFAULT_REFERENCE_CSV,
    epa_mapping: Path = DEFAULT_EPA_MAPPING,
) -> dict[str, Any]:
    rows = build_identity_rows(
        official_csv=official_csv,
        reference_csv=reference_csv,
        epa_mapping=epa_mapping,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    concerns: dict[str, int] = {}
    for row in rows:
        concerns[str(row.get("concern") or "unknown")] = (
            concerns.get(str(row.get("concern") or "unknown"), 0) + 1
        )
    return {
        "output_path": str(output_path),
        "row_count": len(rows),
        "concern_counts": concerns,
    }


def audit_from_match(
    match: DiliIdentityRecord | None,
    *,
    enabled: bool,
    hard_exclude_most: bool,
) -> dict[str, object]:
    if not enabled:
        return {"enabled": False, "status": "disabled", "action": "none"}
    if match is None:
        return {
            "enabled": True,
            "status": "no_exact_match",
            "action": "none",
            "ranking_effect": "none",
            "missing_semantics": "not_a_safety_clearance",
        }
    action = concern_action(match.concern, hard_exclude_most=hard_exclude_most)
    return {
        "enabled": True,
        "status": f"exact_{match.concern}",
        "action": action,
        "concern": match.concern,
        "concern_raw": match.concern_raw,
        "ltkb_id": match.ltkb_id,
        "compound_name": match.compound_name,
        "inchikey": match.inchikey,
        "cas": match.cas,
        "molecule_id": match.molecule_id,
        "match_basis": match.match_basis,
        "source": match.source,
        "ranking_effect": "hard_exclude" if action == "hard_exclude" else "annotation_only",
        "missing_semantics": "not_a_safety_clearance",
    }
