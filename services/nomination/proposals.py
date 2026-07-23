"""Interactive nomination-review proposals for Web human confirmation.

API default path leaves this off. When enabled, proposals are drafts only —
ranking changes apply only after explicit human selection via apply-review.
"""

from __future__ import annotations

import copy
import hashlib
import os
import pickle
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

from packages.models import ScoreRecord
from services.nomination.review import _relabel_tiers, _sort_primary_by_selection
from services.pipeline.export import rows_from_top, to_csv_text
from services.pipeline.run_identity import selection_sha256


@dataclass
class InteractiveReviewProposal:
    proposal_id: str
    molecule_id: str
    severity: str
    issue_type: str
    summary: str
    suggested_action: str  # drop_from_primary | annotate | keep
    evidence_ids: list[str] = field(default_factory=list)
    replacement_molecule_id: str = ""
    replacement_candidates: list[str] = field(default_factory=list)
    source: str = "rules"
    requires_human_confirm: bool = True
    default_selected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class InteractiveReviewBundle:
    enabled: bool
    proposals: list[InteractiveReviewProposal]
    llm_used: bool = False
    draft_engine: str = "rules"
    note: str = ""
    conclusion: str = ""
    intro: str = ""
    narrative_markdown: str = ""
    seat_decisions: list[dict[str, Any]] = field(default_factory=list)
    summary_counts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "requires_human_confirm": True,
            "llm_used": self.llm_used,
            "draft_engine": self.draft_engine,
            "note": self.note,
            "conclusion": self.conclusion,
            "intro": self.intro,
            "narrative_markdown": self.narrative_markdown,
            "seat_decisions": list(self.seat_decisions),
            "summary_counts": dict(self.summary_counts),
            "proposals": [p.to_dict() for p in self.proposals],
            "applied": False,
        }


@dataclass
class InteractiveApplyResult:
    top: list[ScoreRecord]
    reserve: list[ScoreRecord]
    applied_proposal_ids: list[str]
    actions: list[dict[str, Any]]


_SESSION_LOCK = Lock()
_REVIEW_SESSIONS: dict[str, dict[str, Any]] = {}
_SESSION_TTL_SEC = 3600
_SESSION_MAX = 32
_SAFE_RUN_ID = re.compile(r"[^A-Za-z0-9._-]+")


def _session_dir() -> Path:
    override = (os.environ.get("MOLMIND_REVIEW_SESSION_DIR") or "").strip()
    if override:
        path = Path(override)
    else:
        path = Path(tempfile.gettempdir()) / "molmind_review_sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _session_path(run_id: str) -> Path:
    safe = _SAFE_RUN_ID.sub("_", str(run_id or "").strip())[:120] or "unknown"
    return _session_dir() / f"{safe}.pkl"


def _write_session_disk(run_id: str, payload: dict[str, Any]) -> None:
    path = _session_path(run_id)
    tmp = path.with_suffix(".pkl.tmp")
    tmp.write_bytes(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL))
    tmp.replace(path)


def _read_session_disk(run_id: str) -> dict[str, Any] | None:
    path = _session_path(run_id)
    if not path.is_file():
        return None
    try:
        payload = pickle.loads(path.read_bytes())
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    created = float(payload.get("created_at") or 0)
    if time.time() - created > _SESSION_TTL_SEC:
        path.unlink(missing_ok=True)
        return None
    return payload


def _delete_session_disk(run_id: str) -> None:
    _session_path(run_id).unlink(missing_ok=True)


def _evidence_ids(mol: ScoreRecord) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for attr in mol.attributions or []:
        eid = str(getattr(attr, "evidence_id", "") or "")
        if eid and eid not in seen:
            ids.append(eid)
            seen.add(eid)
    for hit in mol.evidence_hits or []:
        eid = str(getattr(hit, "evidence_id", "") or "")
        if eid and eid not in seen:
            ids.append(eid)
            seen.add(eid)
    return ids


def _proposal_id(molecule_id: str, issue_type: str, action: str) -> str:
    raw = f"{molecule_id}|{issue_type}|{action}".encode("utf-8")
    return "pr_" + hashlib.sha1(raw).hexdigest()[:12]


def _next_replacement(
    reserve: list[ScoreRecord],
    *,
    claimed: set[str],
) -> tuple[str, list[str]]:
    candidates = [m.molecule_id for m in reserve if m.molecule_id not in claimed]
    if not candidates:
        return "", []
    return candidates[0], candidates[:5]


def _proposals_from_llm_seats(
    seats: list[dict[str, Any]],
    *,
    top: list[ScoreRecord],
    reserve: list[ScoreRecord],
) -> list[InteractiveReviewProposal]:
    """Map KEEP / KEEP+NOTE / DROP seats into confirmable proposals."""
    by_id = {m.molecule_id: m for m in top}
    claimed: set[str] = set()
    out: list[InteractiveReviewProposal] = []
    for seat in seats:
        mid = str(seat.get("molecule_id") or "")
        if mid not in by_id:
            continue
        decision = str(seat.get("decision") or "KEEP+NOTE")
        rationale = str(seat.get("rationale") or "").strip()
        label = str(seat.get("identity_label") or "")
        issues = seat.get("issue_types") if isinstance(seat.get("issue_types"), list) else []
        issue_type = str(issues[0]) if issues else (
            "llm_drop" if decision == "DROP" else ("llm_annotate" if decision == "KEEP+NOTE" else "llm_keep")
        )
        severity = str(seat.get("severity") or "medium")
        eids = _evidence_ids(by_id[mid])
        if decision == "DROP":
            repl, cands = _next_replacement(reserve, claimed=claimed)
            if repl:
                claimed.add(repl)
            out.append(
                InteractiveReviewProposal(
                    proposal_id=_proposal_id(mid, issue_type, "drop_from_primary"),
                    molecule_id=mid,
                    severity="high",
                    issue_type=issue_type,
                    summary=(
                        f"[{label}] DROP — {rationale}"
                        if rationale
                        else f"[{label}] 建议移出主榜"
                    ),
                    suggested_action="drop_from_primary",
                    evidence_ids=eids,
                    replacement_molecule_id=repl,
                    replacement_candidates=cands,
                    source="llm",
                    default_selected=True,
                )
            )
        elif decision == "KEEP":
            out.append(
                InteractiveReviewProposal(
                    proposal_id=_proposal_id(mid, issue_type, "keep"),
                    molecule_id=mid,
                    severity="low",
                    issue_type=issue_type,
                    summary=(f"[{label}] KEEP — {rationale}" if rationale else f"[{label}] 建议保留"),
                    suggested_action="keep",
                    evidence_ids=eids,
                    source="llm",
                    default_selected=False,
                )
            )
        else:
            out.append(
                InteractiveReviewProposal(
                    proposal_id=_proposal_id(mid, issue_type, "annotate"),
                    molecule_id=mid,
                    severity=severity if severity in {"low", "medium", "high"} else "medium",
                    issue_type=issue_type,
                    summary=(
                        f"[{label}] KEEP+NOTE — {rationale}"
                        if rationale
                        else f"[{label}] 建议脚注保留"
                    ),
                    suggested_action="annotate",
                    evidence_ids=eids,
                    source="llm",
                    default_selected=True,
                )
            )
    return out


def _build_rules_interactive_review(
    top: list[ScoreRecord],
    reserve: list[ScoreRecord],
    *,
    note_parts: list[str] | None = None,
) -> InteractiveReviewBundle:
    proposals: list[InteractiveReviewProposal] = []
    claimed_replacements: set[str] = set()
    notes = list(note_parts or [])

    for mol in top:
        heads = mol.tox_heads or {}
        alert = float(heads.get("alert") or 0.0)
        tox_evidence = float(heads.get("evidence") or 0.0)
        epa = mol.epa_audit or {}
        risk_tier = str(epa.get("cytotox_risk_tier") or "")
        mapping_basis = str(epa.get("mapping_basis") or "")
        eids = _evidence_ids(mol)
        factors = mol.selection_factors or {}

        if alert >= 0.2:
            action = "drop_from_primary" if alert >= 0.85 else "annotate"
            repl, cands = ("", [])
            if action == "drop_from_primary":
                repl, cands = _next_replacement(reserve, claimed=claimed_replacements)
                if repl:
                    claimed_replacements.add(repl)
            proposals.append(
                InteractiveReviewProposal(
                    proposal_id=_proposal_id(mol.molecule_id, "structure_alert", action),
                    molecule_id=mol.molecule_id,
                    severity="high" if alert >= 0.85 else ("medium" if alert >= 0.25 else "low"),
                    issue_type="structure_alert",
                    summary=(
                        f"结构警示 tox_alert={alert:.3f}；"
                        f"{'建议移出主榜并由 reserve 补位' if action == 'drop_from_primary' else '建议脚注保留，实验重点关注 viability'}"
                    ),
                    suggested_action=action,
                    evidence_ids=eids,
                    replacement_molecule_id=repl,
                    replacement_candidates=cands,
                    default_selected=False,
                )
            )

        if tox_evidence >= 0.25:
            proposals.append(
                InteractiveReviewProposal(
                    proposal_id=_proposal_id(
                        mol.molecule_id, "external_tox_evidence", "annotate"
                    ),
                    molecule_id=mol.molecule_id,
                    severity="medium" if tox_evidence >= 0.3 else "low",
                    issue_type="external_tox_evidence",
                    summary=(
                        f"外部毒性证据分量 tox_evidence={tox_evidence:.3f}；"
                        "建议人工核对来源（如 PubChem GHS）并脚注主张上限"
                    ),
                    suggested_action="annotate",
                    evidence_ids=eids,
                )
            )

        if risk_tier == "weak_risk_review":
            proposals.append(
                InteractiveReviewProposal(
                    proposal_id=_proposal_id(
                        mol.molecule_id, "epa_weak_risk_review", "annotate"
                    ),
                    molecule_id=mol.molecule_id,
                    severity="low",
                    issue_type="epa_weak_risk_review",
                    summary=(
                        "EPA 标记 weak_risk_review（未自动降级）；"
                        "建议脚注：弱细胞毒信号仅审计、不等同强风险"
                    ),
                    suggested_action="annotate",
                    evidence_ids=eids,
                )
            )

        query_status = str(epa.get("query_status") or "").lower()
        mapping_status = str(epa.get("mapping_status") or "").lower()
        identity_needs_review = (
            mol.scientific_status == "identity_review_required"
            or query_status == "identity_review_required"
            or "cas" in mapping_basis.lower()
            or mapping_status in {"identifier_match_requires_structure_audit", "cas_only"}
            or str(epa.get("identity_status") or "") in {"review_required", "cas_only"}
        )
        if identity_needs_review:
            proposals.append(
                InteractiveReviewProposal(
                    proposal_id=_proposal_id(
                        mol.molecule_id, "identity_audit", "annotate"
                    ),
                    molecule_id=mol.molecule_id,
                    severity="medium",
                    issue_type="identity_audit",
                    summary=(
                        f"身份映射待审（mapping_basis={mapping_basis or 'n/a'}；"
                        f"query_status={query_status or 'n/a'}）；"
                        "建议核对 CAS/InChIKey，但不因身份审计单独否决"
                    ),
                    suggested_action="annotate",
                    evidence_ids=eids,
                )
            )

        review_note = str(factors.get("nomination_review_reason") or "")
        if review_note:
            proposals.append(
                InteractiveReviewProposal(
                    proposal_id=_proposal_id(
                        mol.molecule_id, "frozen_review_note", "annotate"
                    ),
                    molecule_id=mol.molecule_id,
                    severity="low",
                    issue_type="frozen_review_note",
                    summary=f"仓库已固化复核脚注：{review_note}",
                    suggested_action="annotate",
                    evidence_ids=eids,
                    default_selected=True,
                )
            )

    seen_ids: set[str] = set()
    unique: list[InteractiveReviewProposal] = []
    for row in proposals:
        if row.proposal_id in seen_ids:
            continue
        seen_ids.add(row.proposal_id)
        unique.append(row)

    return InteractiveReviewBundle(
        enabled=True,
        proposals=unique,
        llm_used=False,
        draft_engine="rules",
        note="; ".join(notes) or "规则草案；须人工勾选后才会改动主榜",
    )


def build_interactive_review_proposals(
    top: list[ScoreRecord],
    reserve: list[ScoreRecord],
    *,
    use_llm: bool = False,
    llm_cfg: dict[str, Any] | None = None,
) -> InteractiveReviewBundle:
    """Build human-confirmable draft proposals.

    When ``use_llm`` is True, attempt an evidence-bound LLM seat checklist first;
    on failure or if LLM is not ready, fall back to deterministic rules.
    """
    if use_llm:
        try:
            from services.nomination.llm_review import run_llm_nomination_review

            parsed = run_llm_nomination_review(top, reserve, llm_cfg=llm_cfg)
            proposals = _proposals_from_llm_seats(
                parsed.get("seats") or [],
                top=top,
                reserve=reserve,
            )
            summary = parsed.get("summary") or {}
            return InteractiveReviewBundle(
                enabled=True,
                proposals=proposals,
                llm_used=True,
                draft_engine="llm",
                note="LLM 逐席复核草案；须人工勾选确认后才会改动主榜并导出最终结果",
                conclusion=str(parsed.get("conclusion") or ""),
                intro=str(parsed.get("intro") or ""),
                narrative_markdown=str(parsed.get("narrative_markdown") or ""),
                seat_decisions=list(parsed.get("seats") or []),
                summary_counts={
                    "keep": int(summary.get("keep") or 0),
                    "keep_note": int(summary.get("keep_note") or 0),
                    "drop": int(summary.get("drop") or 0),
                    "extra_notes": list(summary.get("extra_notes") or []),
                },
            )
        except Exception as exc:  # noqa: BLE001 — LLM optional path
            return _build_rules_interactive_review(
                top,
                reserve,
                note_parts=[
                    f"LLM 草案不可用（{exc}）；已回退规则草案",
                ],
            )

    return _build_rules_interactive_review(top, reserve)


def apply_selected_proposals(
    *,
    top: list[ScoreRecord],
    reserve: list[ScoreRecord],
    proposals: Iterable[InteractiveReviewProposal | dict[str, Any]],
    selected_proposal_ids: Iterable[str],
    top_n: int | None = None,
    reserve_n: int | None = None,
) -> InteractiveApplyResult:
    """Apply human-selected proposals; unselected drafts are ignored."""
    selected = {str(x) for x in selected_proposal_ids if str(x).strip()}
    proposal_map: dict[str, InteractiveReviewProposal] = {}
    for raw in proposals:
        if isinstance(raw, InteractiveReviewProposal):
            prop = raw
        else:
            prop = InteractiveReviewProposal(
                proposal_id=str(raw.get("proposal_id") or ""),
                molecule_id=str(raw.get("molecule_id") or ""),
                severity=str(raw.get("severity") or "medium"),
                issue_type=str(raw.get("issue_type") or ""),
                summary=str(raw.get("summary") or ""),
                suggested_action=str(raw.get("suggested_action") or "annotate"),
                evidence_ids=[str(x) for x in (raw.get("evidence_ids") or [])],
                replacement_molecule_id=str(raw.get("replacement_molecule_id") or ""),
                replacement_candidates=[
                    str(x) for x in (raw.get("replacement_candidates") or [])
                ],
                source=str(raw.get("source") or "rules"),
                requires_human_confirm=bool(raw.get("requires_human_confirm", True)),
                default_selected=bool(raw.get("default_selected", False)),
            )
        if prop.proposal_id:
            proposal_map[prop.proposal_id] = prop

    new_top = copy.deepcopy(list(top))
    new_reserve = copy.deepcopy(list(reserve))
    n = top_n if top_n is not None else len(new_top)
    rn = reserve_n if reserve_n is not None else len(new_reserve)
    actions: list[dict[str, Any]] = []
    applied_ids: list[str] = []

    # Process drops first (seat changes), then annotates.
    ordered = sorted(
        (proposal_map[pid] for pid in selected if pid in proposal_map),
        key=lambda p: 0 if p.suggested_action == "drop_from_primary" else 1,
    )

    for prop in ordered:
        action = prop.suggested_action
        mid = prop.molecule_id
        if action == "keep":
            actions.append(
                {
                    "proposal_id": prop.proposal_id,
                    "molecule_id": mid,
                    "action": "keep",
                    "applied": True,
                    "replaced_by": "",
                }
            )
            applied_ids.append(prop.proposal_id)
            continue
        if action == "annotate":
            for mol in new_top + new_reserve:
                if mol.molecule_id != mid:
                    continue
                mol.selection_factors = dict(mol.selection_factors or {})
                mol.selection_factors["interactive_review"] = "annotate"
                mol.selection_factors["interactive_review_reason"] = prop.summary
            actions.append(
                {
                    "proposal_id": prop.proposal_id,
                    "molecule_id": mid,
                    "action": "annotate",
                    "applied": True,
                    "replaced_by": "",
                }
            )
            applied_ids.append(prop.proposal_id)
            continue
        if action != "drop_from_primary":
            actions.append(
                {
                    "proposal_id": prop.proposal_id,
                    "molecule_id": mid,
                    "action": action,
                    "applied": False,
                    "reason": "unsupported_action",
                    "replaced_by": "",
                }
            )
            continue
        idx = next((i for i, mol in enumerate(new_top) if mol.molecule_id == mid), None)
        if idx is None:
            actions.append(
                {
                    "proposal_id": prop.proposal_id,
                    "molecule_id": mid,
                    "action": action,
                    "applied": False,
                    "reason": "not_in_primary",
                    "replaced_by": "",
                }
            )
            continue
        removed = new_top.pop(idx)
        removed.nomination_tier = "review_dropped"
        removed.primary_rank = None
        removed.reserve_rank = None
        removed.selection_factors = dict(removed.selection_factors or {})
        removed.selection_factors["interactive_review"] = "drop_from_primary"
        removed.selection_factors["interactive_review_reason"] = prop.summary
        replacement: ScoreRecord | None = None
        wanted = prop.replacement_molecule_id
        if wanted:
            ridx = next(
                (i for i, mol in enumerate(new_reserve) if mol.molecule_id == wanted),
                None,
            )
            if ridx is not None:
                replacement = new_reserve.pop(ridx)
        if replacement is None and new_reserve:
            replacement = new_reserve.pop(0)
        if replacement is not None:
            replacement.replacement_for = removed.molecule_id
            replacement.selection_factors = dict(replacement.selection_factors or {})
            replacement.selection_factors["interactive_review"] = "promoted_from_reserve"
            replacement.selection_factors["replacement_for"] = removed.molecule_id
            # Append then re-sort: do not inherit the vacated primary seat.
            new_top.append(replacement)
        actions.append(
            {
                "proposal_id": prop.proposal_id,
                "molecule_id": mid,
                "action": action,
                "applied": True,
                "replaced_by": replacement.molecule_id if replacement else "",
            }
        )
        applied_ids.append(prop.proposal_id)

    new_top = _sort_primary_by_selection(new_top)[:n]
    new_reserve = new_reserve[:rn]
    _relabel_tiers(new_top, new_reserve, top_n=max(1, n))
    return InteractiveApplyResult(
        top=new_top,
        reserve=new_reserve,
        applied_proposal_ids=applied_ids,
        actions=actions,
    )


def _proposal_as_dict(row: InteractiveReviewProposal | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, InteractiveReviewProposal):
        return row.to_dict()
    return dict(row)


def store_review_session(
    run_id: str,
    *,
    top: list[ScoreRecord],
    reserve: list[ScoreRecord],
    proposals: list[InteractiveReviewProposal | dict[str, Any]],
    mode: str,
    config_hash: str,
    input_sha256: str,
    degraded_channels: list[str] | tuple[str, ...] | None = None,
    summary: dict[str, Any] | None = None,
    source_filename: str = "",
    llm_cfg: dict[str, Any] | None = None,
    assumptions: dict[str, Any] | None = None,
    hepg2_ffa_resources: dict[str, Any] | None = None,
    logs: list[dict[str, Any]] | None = None,
) -> None:
    """Persist provisional board until human confirm (memory + disk).

    Disk backing survives uvicorn --reload and multi-worker routing on the same
    host, which previously caused apply-review 404s on in-memory misses.
    """
    if not str(run_id or "").strip():
        raise ValueError("run_id required to store review session")
    payload = {
        "created_at": time.time(),
        "top": copy.deepcopy(top),
        "reserve": copy.deepcopy(reserve),
        "proposals": [_proposal_as_dict(p) for p in proposals],
        "mode": mode,
        "config_hash": config_hash,
        "input_sha256": input_sha256,
        "degraded_channels": list(degraded_channels or []),
        "summary": dict(summary or {}),
        "source_filename": str(source_filename or ""),
        "llm_cfg": dict(llm_cfg or {}),
        "assumptions": dict(assumptions or {}),
        "hepg2_ffa_resources": dict(hepg2_ffa_resources or {}),
        "logs": list(logs or []),
    }
    with _SESSION_LOCK:
        _prune_sessions_locked()
        _REVIEW_SESSIONS[run_id] = payload
        _write_session_disk(run_id, payload)


def get_review_session(run_id: str) -> dict[str, Any] | None:
    key = str(run_id or "").strip()
    if not key:
        return None
    with _SESSION_LOCK:
        _prune_sessions_locked()
        row = _REVIEW_SESSIONS.get(key)
        if row is None:
            row = _read_session_disk(key)
            if row is not None:
                _REVIEW_SESSIONS[key] = row
        return copy.deepcopy(row) if row else None


def _prune_sessions_locked() -> None:
    now = time.time()
    expired = [
        key
        for key, row in _REVIEW_SESSIONS.items()
        if now - float(row.get("created_at") or 0) > _SESSION_TTL_SEC
    ]
    for key in expired:
        _REVIEW_SESSIONS.pop(key, None)
        _delete_session_disk(key)
    try:
        disk_files = sorted(
            _session_dir().glob("*.pkl"),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError:
        disk_files = []
    for path in disk_files:
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age > _SESSION_TTL_SEC:
            path.unlink(missing_ok=True)
    if len(_REVIEW_SESSIONS) <= _SESSION_MAX:
        return
    ordered = sorted(
        _REVIEW_SESSIONS.items(),
        key=lambda item: float(item[1].get("created_at") or 0),
    )
    for key, _ in ordered[: max(0, len(ordered) - _SESSION_MAX)]:
        _REVIEW_SESSIONS.pop(key, None)
        _delete_session_disk(key)


def payload_from_applied(
    *,
    run_id: str,
    top: list[ScoreRecord],
    reserve: list[ScoreRecord],
    mode: str,
    config_hash: str,
    input_sha256: str,
    degraded_channels: list[str] | tuple[str, ...] | None,
    base_summary: dict[str, Any],
    interactive_review: dict[str, Any],
    mechanism_job_id: str = "",
    logs: list[dict] | None = None,
    mechanism_graphs: list[dict[str, Any]] | None = None,
    hepg2_ffa_resources: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sel = selection_sha256(top)
    reserve_sel = selection_sha256(reserve)
    summary = dict(base_summary)
    summary.update(
        {
            "output_count": len(top),
            "reserve_count": len(reserve),
            "selection_sha256": sel,
            "reserve_selection_sha256": reserve_sel,
            "mechanism_job_id": mechanism_job_id,
            "review_pending": False,
            "nomination_review": True,
        }
    )
    return {
        "summary": summary,
        "rows": rows_from_top(
            top,
            mode=mode,
            config_hash=config_hash,
            degraded_channels=list(degraded_channels or []),
            run_id=run_id,
            input_sha256=input_sha256,
            selection_hash=sel,
        ),
        "reserve_rows": rows_from_top(
            reserve,
            mode=mode,
            config_hash=config_hash,
            degraded_channels=list(degraded_channels or []),
            run_id=run_id,
            input_sha256=input_sha256,
            selection_hash=reserve_sel,
        ),
        "csv": to_csv_text(
            top,
            mode=mode,
            config_hash=config_hash,
            degraded_channels=list(degraded_channels or []),
            run_id=run_id,
            input_sha256=input_sha256,
            selection_hash=sel,
        ),
        "logs": list(logs or []),
        "mechanism_graphs": list(mechanism_graphs or []),
        "hepg2_ffa_resources": dict(hepg2_ffa_resources or {}),
        "mechanism_job_id": mechanism_job_id,
        "mechanism_md": "",
        "mechanism_pdf_base64": "",
        "mechanism_pdf_name": "",
        "interactive_review": interactive_review,
    }
