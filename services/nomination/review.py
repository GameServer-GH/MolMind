"""Deterministic clinical exclusion and nomination-review helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from packages.models import CriticAction, MoleculeRecord, ScoreRecord
from services.ranker.ranker import competition_selection_score


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _norm_upper(value: Any) -> str:
    return _norm(value).upper()


@dataclass(frozen=True)
class ClinicalExclusionHit:
    exclusion_id: str
    reason: str
    matched_by: str
    matched_value: str


@dataclass
class NominationReviewAction:
    molecule_id: str
    action: str
    reason: str
    replaced_by: str = ""
    applied: bool = False
    scope: str = "decision"  # decision | bundle


@dataclass
class NominationReviewResult:
    algorithmic_top: list[ScoreRecord]
    algorithmic_reserve: list[ScoreRecord]
    nominated_top: list[ScoreRecord]
    nominated_reserve: list[ScoreRecord]
    actions: list[NominationReviewAction] = field(default_factory=list)
    input_matched: bool = True
    review_applied: bool = True


def load_clinical_exclusions(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    cfg = dict(config or {})
    if not bool(cfg.get("enabled", True)):
        return []
    rows = []
    for row in cfg.get("exclusions") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("action") or "hard_exclude") != "hard_exclude":
            continue
        rows.append(row)
    return rows


def match_clinical_exclusion(
    record: MoleculeRecord,
    exclusions: Iterable[dict[str, Any]],
    *,
    scored: ScoreRecord | None = None,
) -> ClinicalExclusionHit | None:
    molecule_id = _norm(record.molecule_id)
    cas = _norm(record.cas)
    inchikey = _norm(record.inchikey)
    smiles = _norm(record.smiles)
    for row in exclusions:
        exclusion_id = _norm(row.get("id")) or "unnamed_exclusion"
        reason = _norm(row.get("reason")) or "clinical_exclusion"
        for mid in row.get("molecule_ids") or []:
            if molecule_id and molecule_id == _norm(mid):
                return ClinicalExclusionHit(
                    exclusion_id, reason, "molecule_id", molecule_id
                )
        for item in row.get("cas") or []:
            if cas and cas == _norm(item):
                return ClinicalExclusionHit(exclusion_id, reason, "cas", cas)
        for item in row.get("inchikeys") or []:
            key = _norm_upper(item)
            if inchikey and inchikey.upper() == key:
                return ClinicalExclusionHit(exclusion_id, reason, "inchikey", inchikey)
            if scored is not None:
                epa = scored.epa_audit or {}
                for field_name in ("original_inchikey", "standardized_inchikey"):
                    value = _norm(epa.get(field_name))
                    if value and value.upper() == key:
                        return ClinicalExclusionHit(
                            exclusion_id, reason, field_name, value
                        )
        for item in row.get("standardized_smiles") or []:
            if smiles and smiles == _norm(item):
                return ClinicalExclusionHit(
                    exclusion_id, reason, "standardized_smiles", smiles
                )
    return None


def apply_clinical_exclusion_to_score(
    scored: ScoreRecord,
    record: MoleculeRecord,
    exclusions: Iterable[dict[str, Any]],
) -> CriticAction | None:
    hit = match_clinical_exclusion(record, exclusions, scored=scored)
    if hit is None:
        return None
    reasons = tuple(scored.eligibility_reasons or ())
    if "clinical_exclusion" not in reasons:
        reasons = reasons + ("clinical_exclusion",)
    scored.eligibility_status = "ineligible"
    scored.eligibility_reasons = reasons
    scored.gated_out = True
    scored.gate_reason = (
        f"clinical_exclusion:{hit.exclusion_id} matched_by={hit.matched_by} "
        f"({hit.matched_value}); {hit.reason}"
    )
    scored.selection_factors = dict(scored.selection_factors or {})
    scored.selection_factors["clinical_exclusion"] = hit.exclusion_id
    scored.selection_factors["clinical_exclusion_match"] = (
        f"{hit.matched_by}={hit.matched_value}"
    )
    return CriticAction(
        action="drop",
        molecule_id=scored.molecule_id,
        reason=scored.gate_reason,
        original_status="scored",
        checks_performed=("clinical_exclusion",),
        score_before=scored.final_score,
        score_after=0.0,
        eligibility_before="eligible",
        eligibility_after="ineligible",
        final_decision="not_selected",
    )


def _allowed_input_hashes(cfg: dict[str, Any]) -> set[str]:
    raw = cfg.get("applies_to_input_sha256")
    if raw is None or raw == "":
        return set()
    if isinstance(raw, (list, tuple, set)):
        return {_norm_upper(item) for item in raw if _norm(item)}
    return {_norm_upper(raw)}


def nomination_review_applies_to_input(
    review_config: dict[str, Any] | None,
    *,
    input_sha256: str,
) -> tuple[bool, str]:
    """Return whether ID-scoped nomination review should run for this SDF."""
    cfg = dict(review_config or {})
    if not bool(cfg.get("enabled", True)):
        return False, "nomination_review_disabled"
    allowed = _allowed_input_hashes(cfg)
    require_match = bool(cfg.get("require_input_match", bool(allowed)))
    if not require_match or not allowed:
        return True, "unbound_or_match_not_required"
    current = _norm_upper(input_sha256)
    if current and current in allowed:
        return True, "input_sha256_matched"
    return (
        False,
        "input_sha256_mismatch; ID-scoped nomination_review skipped; "
        "clinical_exclusions still apply",
    )


def _decision_target_ids(
    row: dict[str, Any],
    candidates: Iterable[ScoreRecord],
) -> list[str]:
    """Resolve a review decision onto current-library molecule IDs."""
    explicit = _norm(row.get("molecule_id"))
    wanted_ids = {_norm(item) for item in (row.get("molecule_ids") or []) if _norm(item)}
    if explicit:
        wanted_ids.add(explicit)
    wanted_cas = {_norm(item) for item in (row.get("cas") or []) if _norm(item)}
    wanted_ik = {_norm_upper(item) for item in (row.get("inchikeys") or []) if _norm(item)}

    matched: list[str] = []
    seen: set[str] = set()
    for mol in candidates:
        mid = _norm(mol.molecule_id)
        cas = _norm(mol.cas)
        inchikey = _norm_upper(mol.inchikey)
        epa = mol.epa_audit or {}
        epa_keys = {
            _norm_upper(epa.get("original_inchikey")),
            _norm_upper(epa.get("standardized_inchikey")),
        }
        hit = False
        if mid and mid in wanted_ids:
            hit = True
        elif cas and cas in wanted_cas:
            hit = True
        elif inchikey and inchikey in wanted_ik:
            hit = True
        elif wanted_ik and (epa_keys & wanted_ik):
            hit = True
        if hit and mid and mid not in seen:
            matched.append(mid)
            seen.add(mid)
    return matched


def _sort_primary_by_selection(top: list[ScoreRecord]) -> list[ScoreRecord]:
    """Re-rank primary after reserve promotion; do not keep vacated seat order."""
    return sorted(
        top,
        key=lambda mol: (-competition_selection_score(mol), mol.molecule_id),
    )


def _relabel_tiers(
    top: list[ScoreRecord],
    reserve: list[ScoreRecord],
    *,
    top_n: int,
) -> None:
    for rank, mol in enumerate(top, start=1):
        mol.nomination_tier = "primary"
        mol.primary_rank = rank
        mol.reserve_rank = None
        if not mol.replacement_for:
            mol.replacement_for = ""
    for rank, mol in enumerate(reserve, start=1):
        mol.nomination_tier = "reserve"
        mol.primary_rank = None
        mol.reserve_rank = rank
        if not mol.replacement_for:
            mol.replacement_for = f"primary_slot_{((rank - 1) % max(1, top_n)) + 1}"


def apply_nomination_review(
    *,
    algorithmic_top: list[ScoreRecord],
    algorithmic_reserve: list[ScoreRecord],
    leftover_pool: list[ScoreRecord] | None = None,
    review_config: dict[str, Any] | None,
    mode: str,
    top_n: int,
    reserve_n: int,
    input_sha256: str = "",
) -> NominationReviewResult:
    """Apply declared nomination decisions after algorithmic primary/reserve split.

    When ``applies_to_input_sha256`` is set and the current SDF hash differs,
    ID-scoped review is skipped so a delivered repo remains safe on other
    libraries. Chemistry hard gates remain in ``clinical_exclusions.yaml``.
    """
    cfg = dict(review_config or {})
    applies, apply_reason = nomination_review_applies_to_input(
        cfg, input_sha256=input_sha256
    )
    if not applies:
        return NominationReviewResult(
            algorithmic_top=list(algorithmic_top),
            algorithmic_reserve=list(algorithmic_reserve),
            nominated_top=list(algorithmic_top),
            nominated_reserve=list(algorithmic_reserve),
            actions=[
                NominationReviewAction(
                    molecule_id="*",
                    action="skip_review_bundle",
                    reason=apply_reason,
                    applied=False,
                    scope="bundle",
                )
            ],
            input_matched=False,
            review_applied=False,
        )

    decisions = [row for row in (cfg.get("decisions") or []) if isinstance(row, dict)]
    apply_modes = {
        str(item).lower() for item in (cfg.get("apply_in_modes") or ["auto", "offline", "online"])
    }
    seat_changes_enabled = bool(cfg.get("enabled", True)) and mode.lower() in apply_modes

    top = list(algorithmic_top)
    reserve = list(algorithmic_reserve)
    leftover = list(leftover_pool or [])
    actions: list[NominationReviewAction] = []
    universe = list(top) + list(reserve) + list(leftover)

    for row in decisions:
        action = _norm(row.get("action")).lower() or "annotate"
        reason = _norm(row.get("reason")) or action
        target_ids = _decision_target_ids(row, universe)
        if not target_ids:
            actions.append(
                NominationReviewAction(
                    molecule_id=_norm(row.get("molecule_id")) or "*",
                    action=action,
                    reason=f"no_matching_candidate_in_current_library; {reason}",
                    applied=False,
                )
            )
            continue

        for molecule_id in target_ids:
            if action == "annotate":
                for mol in universe:
                    if mol.molecule_id != molecule_id:
                        continue
                    mol.selection_factors = dict(mol.selection_factors or {})
                    mol.selection_factors["nomination_review"] = "annotate"
                    mol.selection_factors["nomination_review_reason"] = reason
                actions.append(
                    NominationReviewAction(
                        molecule_id=molecule_id,
                        action=action,
                        reason=reason,
                        applied=True,
                    )
                )
                continue
            if action != "drop_from_primary":
                actions.append(
                    NominationReviewAction(
                        molecule_id=molecule_id,
                        action=action,
                        reason=f"unsupported_action:{action}; {reason}",
                        applied=False,
                    )
                )
                continue
            if not seat_changes_enabled:
                actions.append(
                    NominationReviewAction(
                        molecule_id=molecule_id,
                        action=action,
                        reason=f"seat_change_skipped_for_mode={mode}; {reason}",
                        applied=False,
                    )
                )
                continue
            idx = next((i for i, mol in enumerate(top) if mol.molecule_id == molecule_id), None)
            if idx is None:
                actions.append(
                    NominationReviewAction(
                        molecule_id=molecule_id,
                        action=action,
                        reason=f"not_in_algorithmic_primary; {reason}",
                        applied=False,
                    )
                )
                continue
            removed = top.pop(idx)
            removed.nomination_tier = "review_dropped"
            removed.primary_rank = None
            removed.reserve_rank = None
            removed.selection_factors = dict(removed.selection_factors or {})
            removed.selection_factors["nomination_review"] = "drop_from_primary"
            removed.selection_factors["nomination_review_reason"] = reason
            replacement: ScoreRecord | None = None
            if reserve:
                replacement = reserve.pop(0)
                replacement.replacement_for = removed.molecule_id
                replacement.selection_factors = dict(replacement.selection_factors or {})
                replacement.selection_factors["nomination_review"] = "promoted_from_reserve"
                replacement.selection_factors["replacement_for"] = removed.molecule_id
                # Append then re-sort: do not inherit the vacated primary seat.
                top.append(replacement)
            while len(reserve) < reserve_n and leftover:
                nxt = leftover.pop(0)
                if nxt.molecule_id in {m.molecule_id for m in top} | {
                    m.molecule_id for m in reserve
                }:
                    continue
                nxt.selection_factors = dict(nxt.selection_factors or {})
                nxt.selection_factors["nomination_review"] = "reserve_backfill"
                reserve.append(nxt)
            actions.append(
                NominationReviewAction(
                    molecule_id=molecule_id,
                    action=action,
                    reason=reason,
                    replaced_by=replacement.molecule_id if replacement else "",
                    applied=True,
                )
            )

    top = _sort_primary_by_selection(top)[:top_n]
    reserve = reserve[:reserve_n]
    _relabel_tiers(top, reserve, top_n=top_n)
    return NominationReviewResult(
        algorithmic_top=list(algorithmic_top),
        algorithmic_reserve=list(algorithmic_reserve),
        nominated_top=top,
        nominated_reserve=reserve,
        actions=actions,
        input_matched=True,
        review_applied=True,
    )
