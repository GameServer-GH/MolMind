"""Local EPA CTX/ToxCast evidence index for staged candidate auditing."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from services.evidence_facade.epa_risk import (
    epa_cytotox_metrics,
    epa_cytotox_risk_tier,
    risk_tier_rank,
)


ROOT = Path(__file__).resolve().parents[2]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.is_file():
        return ()
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return ()
    return rows


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_bulk_artifact(
    path: Path,
    *,
    required_identity_field: str | None = None,
) -> bool:
    """Return true only for a completed, hash-matched bulk artifact."""
    manifest_path = path.with_suffix(".manifest.json")
    if not path.is_file() or not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if str(manifest.get("status") or "") not in {"imported", "completed"}:
            return False
        if required_identity_field and required_identity_field not in set(
            manifest.get("identity_fields") or []
        ):
            return False
        expected_sha256 = str(manifest.get("processed_sha256") or "")
        return bool(expected_sha256) and _sha256(path) == expected_sha256
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _summary_records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return []


def _sum_field(rows: Iterable[dict[str, Any]], field_name: str) -> int:
    return sum(_int(row.get(field_name)) for row in rows)


def _first_float(rows: Iterable[dict[str, Any]], field_name: str) -> float | None:
    for row in rows:
        try:
            value = row.get(field_name)
            if value is None or value == "":
                continue
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


@dataclass
class EPAContextIndex:
    """Candidate-keyed EPA summary index with conservative identity aliases."""

    by_key: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_standardized_smiles: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    source_paths: list[str] = field(default_factory=list)
    loaded_rows: int = 0

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> "EPAContextIndex":
        cfg = dict(config or {})
        if not bool(cfg.get("enabled", True)):
            return cls()
        paths: list[Path] = []
        for item in cfg.get("mapping_paths") or []:
            path = Path(str(item))
            resolved = path if path.is_absolute() else ROOT / path
            # Checkpointed bulk mapping is not a stable evidence snapshot
            # until its manifest, identity fields and content hash agree.
            if "bulk_mapping" in resolved.name and not _stable_bulk_artifact(
                resolved,
                required_identity_field="standardized_inchikey",
            ):
                continue
            paths.append(resolved)
        for item in cfg.get("risk_summary_paths") or []:
            path = Path(str(item))
            resolved = path if path.is_absolute() else ROOT / path
            # The importer appends checkpoints directly to this path. Never
            # expose a partial summary to reports or stage-2 scoring.
            if "bulk_bioactivity_summary" in resolved.name and not _stable_bulk_artifact(
                resolved
            ):
                continue
            paths.append(resolved)
        for item in cfg.get("assay_qc_paths") or []:
            paths.append(Path(str(item)))
        if not paths:
            paths = [
                ROOT / "data/public/processed/epa_ctx/candidate_mapping.jsonl",
                ROOT / "data/public/processed/epa_ctx/candidate_risk_summary.jsonl",
                ROOT / "data/public/processed/epa_toxcast_tox21/records_endpoint_qc.jsonl",
            ]
        return cls.from_paths(paths)

    @classmethod
    def from_paths(cls, paths: Iterable[Path]) -> "EPAContextIndex":
        index = cls()
        paths_unique: list[Path] = []
        seen: set[Path] = set()
        for path in paths:
            resolved = Path(path)
            if not resolved.is_absolute():
                resolved = ROOT / resolved
            if resolved in seen:
                continue
            seen.add(resolved)
            paths_unique.append(resolved)
            if resolved.is_file():
                index.source_paths.append(str(resolved))

        mapping_by_dtxsid: dict[str, dict[str, Any]] = {}
        mapping_rows: list[dict[str, Any]] = []
        risk_by_dtxsid: dict[str, dict[str, Any]] = {}
        assay_by_dtxsid: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for path in paths_unique:
            name = path.name.lower()
            rows = list(_read_jsonl(path))
            index.loaded_rows += len(rows)
            if "candidate_mapping" in name or "bulk_mapping" in name:
                mapping_rows.extend(rows)
            elif "risk_summary" in name or "bioactivity_summary" in name:
                for row in rows:
                    dtxsid = _text(row.get("dtxsid"))
                    if dtxsid:
                        risk_by_dtxsid[dtxsid] = {
                            **risk_by_dtxsid.get(dtxsid, {}),
                            **row,
                        }
            elif "records_endpoint_qc" in name or "records.jsonl" in str(path):
                for row in rows:
                    dtxsid = _text(row.get("dtxsid") or row.get("compound_id"))
                    if dtxsid:
                        assay_by_dtxsid[dtxsid].append(row)

        for mapping in mapping_rows:
            dtxsid = _text(mapping.get("dtxsid"))
            if dtxsid:
                mapping_by_dtxsid[dtxsid] = {
                    **mapping_by_dtxsid.get(dtxsid, {}),
                    **mapping,
                }

        for dtxsid, mapping in mapping_by_dtxsid.items():
            risk = dict(risk_by_dtxsid.get(dtxsid) or {})
            assay_rows = list(assay_by_dtxsid.get(dtxsid) or [])
            summary_payload = risk.get("bioactivity_summary") or risk.get("record") or {}
            summaries = _summary_records(summary_payload)
            if "active_hit_count_hitc_gte_0_9" in risk:
                active_count = _int(risk.get("active_hit_count_hitc_gte_0_9"))
            elif "active_hit_count" in risk:
                active_count = _int(risk.get("active_hit_count"))
            else:
                active_count = _sum_field(summaries, "activeMc") + _sum_field(
                    summaries, "activeSc"
                )
            if "bioactivity_record_count" in risk:
                record_count = _int(risk.get("bioactivity_record_count"))
            else:
                record_count = _sum_field(summaries, "totalMc") + _sum_field(
                    summaries, "totalSc"
                )
            nhit = risk.get("nhit")
            if nhit is None:
                nhit = _first_float(summaries, "nhit")
            cytotox_lower_um = risk.get("cytotox_lower_um")
            if cytotox_lower_um is None:
                cytotox_lower_um = risk.get("cytotoxLowerUm")
            if cytotox_lower_um is None:
                cytotox_lower_um = _first_float(summaries, "cytotoxLowerUm")
            cytotox_median_um = risk.get("cytotox_median_um")
            if cytotox_median_um is None:
                cytotox_median_um = risk.get("cytotoxMedianUm")
            if cytotox_median_um is None:
                cytotox_median_um = _first_float(summaries, "cytotoxMedianUm")
            active_aeids = list(
                risk.get("active_aeids")
                or [
                    row.get("aeid")
                    for row in assay_rows
                    if row.get("active_hit") or row.get("classification") == "active_risk"
                ]
            )
            mapping_status = _text(mapping.get("mapping_status")) or "audit_missing"
            summary_status = _text(risk.get("status"))
            if not summary_status:
                if risk:
                    summary_status = (
                        "verified_empty"
                        if active_count == 0 and record_count == 0
                        else "returned"
                    )
                else:
                    summary_status = "not_queried"
            # Bioactivity presence is auditable but is not by itself a tox score.
            bioactivity_signal = active_count > 0 or bool(assay_rows)
            entry = {
                "source_id": "epa_ctx",
                "dtxsid": dtxsid,
                "dtxcid": _text(mapping.get("dtxcid")),
                "preferred_name": _text(mapping.get("preferred_name")),
                "cas": _text(mapping.get("cas") or mapping.get("casrn")),
                "casrn": _text(mapping.get("casrn") or mapping.get("cas")),
                "original_inchikey": _text(mapping.get("original_inchikey")),
                "standardized_inchikey": _text(mapping.get("standardized_inchikey")),
                "standardized_smiles": _text(mapping.get("standardized_smiles")),
                "mapping_value": _text(mapping.get("mapping_value")),
                "mapping_basis": _text(mapping.get("mapping_basis")),
                "mapping_status": mapping_status,
                "hit_count": _int(mapping.get("hit_count")),
                "summary_status": summary_status,
                "bioactivity_record_count": record_count,
                "active_hit_count": active_count,
                "nhit": nhit if nhit is not None else 0.0,
                "cytotox_lower_um": cytotox_lower_um,
                "cytotox_median_um": cytotox_median_um,
                "active_aeids": active_aeids,
                "toxval_record_count": _int(risk.get("toxval_record_count")),
                "toxref_summary_record_count": _int(risk.get("toxref_summary_record_count")),
                "interpretation": _text(risk.get("interpretation"))
                or (
                    "bioactivity_signal_present"
                    if bioactivity_signal
                    else "audit_missing_or_no_active_hit"
                ),
                "bioactivity_signal": bioactivity_signal,
                # Backward-compatible alias: callers that only need presence.
                "risk_signal": bioactivity_signal,
                "assay_rows": assay_rows[:20],
                "retrieved_at": _text(
                    risk.get("retrieved_at")
                    or mapping.get("retrieved_at")
                    or (assay_rows[0].get("retrieved_at") if assay_rows else "")
                ),
                "source_paths": list(index.source_paths),
            }
            keys = {
                entry["dtxsid"],
                entry["cas"],
                entry["casrn"],
                entry["original_inchikey"],
                entry["standardized_inchikey"],
                entry["mapping_value"],
            }
            for assay in assay_rows:
                keys.update({_text(assay.get("inchikey")), _text(assay.get("cas"))})
            for key in keys:
                if key:
                    index.by_key[key] = entry
            std_smiles = entry["standardized_smiles"]
            if std_smiles:
                index.by_standardized_smiles.setdefault(std_smiles, []).append(entry)

        return index

    def _annotate_match(
        self,
        entry: dict[str, Any],
        *,
        identity_type: str,
        key: str,
    ) -> dict[str, Any]:
        annotated = dict(entry)
        annotated["_matched_key"] = key
        if identity_type == "inchikey":
            if key == _text(entry.get("standardized_inchikey")):
                match_basis = "standardized_inchikey"
            elif key in {
                _text(entry.get("original_inchikey")),
                _text(entry.get("mapping_value")),
            }:
                match_basis = "original_inchikey"
            else:
                match_basis = "assay_inchikey"
        elif identity_type == "standardized_smiles":
            match_basis = "standardized_smiles"
        else:
            match_basis = "cas"
        annotated["_matched_identity_type"] = identity_type
        annotated["_matched_identity_basis"] = match_basis
        return annotated

    def lookup(
        self,
        *,
        inchikey: str = "",
        cas: str | None = None,
        smiles: str | None = None,
        share_standardized_smiles_risk: bool = True,
        screening_um: float = 10.0,
    ) -> dict[str, Any] | None:
        primary: dict[str, Any] | None = None
        for identity_type, key in (
            ("inchikey", _text(inchikey)),
            ("cas", _text(cas)),
        ):
            if key and key in self.by_key:
                primary = self._annotate_match(
                    self.by_key[key], identity_type=identity_type, key=key
                )
                break

        std_smiles = _text(smiles) or _text((primary or {}).get("standardized_smiles"))
        aliases: list[dict[str, Any]] = []
        if std_smiles and std_smiles in self.by_standardized_smiles:
            aliases = list(self.by_standardized_smiles[std_smiles])

        donor: dict[str, Any] | None = None
        if share_standardized_smiles_risk and aliases:
            for alias in aliases:
                if _text(alias.get("mapping_status")) != "exact_identifier_match":
                    continue
                tier = epa_cytotox_risk_tier(alias, screening_um=screening_um)
                if risk_tier_rank(tier) <= 0:
                    continue
                if donor is None or risk_tier_rank(tier) > risk_tier_rank(
                    epa_cytotox_risk_tier(donor, screening_um=screening_um)
                ):
                    donor = alias
                elif (
                    donor is not None
                    and risk_tier_rank(tier)
                    == risk_tier_rank(epa_cytotox_risk_tier(donor, screening_um=screening_um))
                    and float(epa_cytotox_metrics(alias)["nhit"])
                    > float(epa_cytotox_metrics(donor)["nhit"])
                ):
                    donor = alias

        if primary is None and donor is None:
            return None
        if primary is None and donor is not None:
            inherited = self._annotate_match(
                donor,
                identity_type="standardized_smiles",
                key=std_smiles,
            )
            inherited["risk_inherited_from_dtxsid"] = _text(donor.get("dtxsid"))
            inherited["risk_inheritance_basis"] = "standardized_smiles"
            inherited["risk_inheritance_preferred_name"] = _text(donor.get("preferred_name"))
            return inherited

        assert primary is not None
        primary_tier = epa_cytotox_risk_tier(primary, screening_um=screening_um)
        if (
            donor is not None
            and _text(donor.get("dtxsid")) != _text(primary.get("dtxsid"))
            and risk_tier_rank(epa_cytotox_risk_tier(donor, screening_um=screening_um))
            > risk_tier_rank(primary_tier)
        ):
            merged = dict(primary)
            for field_name in (
                "nhit",
                "cytotox_lower_um",
                "cytotox_median_um",
                "active_hit_count",
                "bioactivity_record_count",
                "active_aeids",
                "summary_status",
                "interpretation",
                "bioactivity_signal",
                "risk_signal",
                "assay_rows",
                "retrieved_at",
            ):
                merged[field_name] = donor.get(field_name)
            merged["risk_inherited_from_dtxsid"] = _text(donor.get("dtxsid"))
            merged["risk_inheritance_basis"] = "standardized_smiles"
            merged["risk_inheritance_preferred_name"] = _text(donor.get("preferred_name"))
            merged["risk_donor_mapping_status"] = _text(donor.get("mapping_status"))
            return merged
        return primary

