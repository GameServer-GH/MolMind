"""Evidence-bound LLM draft for Web interactive nomination review.

Does not change ranking by itself — output is a human-confirmable checklist.
Falls back is handled by the caller (rules engine).
"""

from __future__ import annotations

from plugins.molmind_core.scientific.paths import REPO_ROOT
import json
import re
from pathlib import Path
from typing import Any

import yaml

from packages.models import ScoreRecord
from plugins.molmind_core.scientific.mechanism.llm_client import MechanismLLMError, chat_completion, resolve_llm_settings

ROOT = REPO_ROOT

_SYSTEM = """你是药物化学/毒理短名单复核助手。根据「审计证据包」写人工复核草案（供勾选确认，不自动改榜）。

硬性约束：
1. 不得编造证据包中不存在的数值（分数、nhit、CAS、InChIKey、EPA tier 等）。
2. 身份 / EPA / 毒性分头 / claim_ceiling / scientific_status 必须与证据包一致。
3. curated_hints（若有）优先：含撤市/严重过敏等硬药史时，对应席位 decision 必须为 DROP。
4. 可用药学常识补公共名/用途，须标明「常识」；不得写成证据包已验证的实验事实。
5. verified_empty / EPA 缺失 ≠ 安全；weak_risk_review 未自动降级时不要写成强细胞毒。
6. tox_confidence=0.0 在本流水线很常见，不要因此把每席都写成「高不确定性长文」。
7. issue_types 必须与该席真实问题匹配；禁止给所有席位统一贴 epa_weak_risk_review。
8. 决策要有区分度：干净低警示席位用 KEEP；仅 proxy/EPA 空/弱警示用 KEEP+NOTE；撤市或不可接受硬风险用 DROP。不要默认 10 席全 KEEP+NOTE。
9. 文案要短：conclusion≤40字；intro≤60字；每席 rationale≤60字（1–2 短句）；extra_notes≤3 条、每条≤40字。
10. 只输出一个 JSON 对象，不要 Markdown 围栏。

JSON schema：
{
  "conclusion": "一句话结论",
  "intro": "一句导语",
  "seats": [
    {
      "rank": 1,
      "molecule_id": "Txxxx",
      "identity_label": "公共名或骨架（尽量短）",
      "decision": "KEEP | KEEP+NOTE | DROP",
      "rationale": "≤60字要点",
      "issue_types": ["identity_audit"|"epa_weak_risk_review"|"structure_alert"|"external_tox_evidence"|"drug_history"|"claim_ceiling"|"other"],
      "severity": "low|medium|high"
    }
  ],
  "summary": {
    "keep": 0,
    "keep_note": 0,
    "drop": 0,
    "extra_notes": ["汇总补充"]
  }
}

seats 必须覆盖全部 primary，rank 与证据包一致。
"""


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def _curated_hints_for_board(top: list[ScoreRecord]) -> dict[str, dict[str, Any]]:
    """Load ID/CAS/InChIKey hints even when yaml apply switches are disabled."""
    by_id: dict[str, dict[str, Any]] = {}
    clinical = _read_yaml(ROOT / "configs" / "clinical_exclusions.yaml")
    for row in clinical.get("exclusions") or []:
        if not isinstance(row, dict):
            continue
        reason = str(row.get("reason") or "").strip()
        preferred = "DROP" if str(row.get("action") or "") == "hard_exclude" else "KEEP+NOTE"
        hint = {
            "source": "clinical_exclusions",
            "preferred_decision": preferred,
            "label_hint": str(row.get("id") or ""),
            "reason": reason[:280],
        }
        for mid in row.get("molecule_ids") or []:
            by_id[str(mid)] = dict(hint)
        # Also match by inchikey/cas on the live board.
        inchis = {str(x).upper() for x in (row.get("inchikeys") or []) if x}
        cases = {str(x) for x in (row.get("cas") or []) if x}
        for mol in top:
            if mol.molecule_id in by_id:
                continue
            if (mol.inchikey or "").upper() in inchis or (mol.cas or "") in cases:
                by_id[mol.molecule_id] = dict(hint)

    review = _read_yaml(ROOT / "configs" / "nomination_review.yaml")
    for row in review.get("decisions") or []:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("molecule_id") or "")
        if not mid:
            continue
        action = str(row.get("action") or "annotate").lower()
        preferred = "DROP" if action == "drop_from_primary" else "KEEP+NOTE"
        # clinical hard_exclude wins if already present
        if mid in by_id and by_id[mid].get("preferred_decision") == "DROP":
            continue
        reason = str(row.get("reason") or "").strip()
        # Promote known withdrawn language to DROP even if yaml says annotate.
        if "withdrawn" in reason.lower() or "撤市" in reason:
            preferred = "DROP"
        by_id[mid] = {
            "source": "nomination_review",
            "preferred_decision": preferred,
            "label_hint": mid,
            "reason": reason[:280],
        }
    return by_id


def build_seat_evidence_pack(
    top: list[ScoreRecord],
    reserve: list[ScoreRecord],
) -> dict[str, Any]:
    """Compact, JSON-serializable audit pack for the LLM (no fp_bits)."""
    curated = _curated_hints_for_board(top)

    def _mol(mol: ScoreRecord, *, rank: int | None, tier: str) -> dict[str, Any]:
        heads = mol.tox_heads or {}
        epa = mol.epa_audit or {}
        dili = mol.dili_audit or {}
        factors = mol.selection_factors or {}
        payload = {
            "rank": rank if rank is not None else (mol.primary_rank or mol.reserve_rank),
            "tier": tier,
            "molecule_id": mol.molecule_id,
            "cas": mol.cas or "",
            "inchikey": mol.inchikey or "",
            "identity_status": mol.identity_status,
            "scientific_status": mol.scientific_status,
            "claim_ceiling": mol.claim_ceiling,
            "lipid_score": round(float(mol.lipid_score), 3),
            "tox_risk": round(float(mol.tox_risk), 3),
            "final_score": round(float(mol.final_score), 3),
            "conf_e": round(float(mol.conf_e), 3),
            "toxicity_confidence": round(float(mol.toxicity_confidence), 3),
            "tox_heads": {
                "alert": round(float(heads.get("alert") or 0.0), 3),
                "physchem": round(float(heads.get("physchem") or 0.0), 3),
                "dili": round(float(heads.get("dili") or 0.0), 3),
                "admet": round(float(heads.get("admet") or 0.0), 3),
                "evidence": round(float(heads.get("evidence") or 0.0), 3),
            },
            "tox_rationale": (mol.tox_rationale or "")[:220],
            "overall_reason": (mol.overall_reason or "")[:220],
            "nomination_review_reason": str(factors.get("nomination_review_reason") or "")[:200],
            "epa": {
                "query_status": epa.get("query_status"),
                "mapping_status": epa.get("mapping_status"),
                "mapping_basis": epa.get("mapping_basis"),
                "nhit": epa.get("nhit"),
                "cytotox_lower_um": epa.get("cytotox_lower_um"),
                "cytotox_risk_tier": epa.get("cytotox_risk_tier"),
                "risk_applied": epa.get("risk_applied"),
            },
            "dili": {
                "status": dili.get("status"),
                "action": dili.get("action"),
                "concern": dili.get("concern"),
                "compound_name": dili.get("compound_name"),
            },
            "novelty_nearest_reference": mol.novelty_nearest_reference,
            "novelty_max_similarity": round(float(mol.novelty_max_similarity or 0.0), 3),
        }
        hint = curated.get(mol.molecule_id)
        if hint:
            payload["curated_hint"] = hint
        return payload

    return {
        "primary": [
            _mol(m, rank=(m.primary_rank or i), tier="primary")
            for i, m in enumerate(top, start=1)
        ],
        "reserve": [
            _mol(m, rank=(m.reserve_rank or i), tier="reserve")
            for i, m in enumerate(reserve[:5], start=1)
        ],
        "instructions": {
            "screening_um": 10,
            "viability_endpoint": "CCK-8",
            "claim_policy": "proxy_only_cannot_claim_experimental_lipid_lowering",
            "style": "short_checklist_for_human_confirm",
        },
    }


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise MechanismLLMError("LLM 返回空文本")
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        raw = fence.group(1)
    else:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MechanismLLMError(f"LLM JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise MechanismLLMError("LLM JSON 根节点必须是对象")
    return data


def _normalize_decision(raw: str) -> str:
    token = str(raw or "").strip().upper().replace(" ", "").replace("_", "+")
    if token in {"DROP", "DROPFROMPRIMARY", "DROP-FROM-PRIMARY", "移出", "否决"}:
        return "DROP"
    if token in {"KEEP+NOTE", "KEEPNOTE", "ANNOTATE", "NOTE", "脚注"}:
        return "KEEP+NOTE"
    if token in {"KEEP", "保留"}:
        return "KEEP"
    if "DROP" in token or "移出" in token:
        return "DROP"
    if "NOTE" in token or "脚注" in token:
        return "KEEP+NOTE"
    return "KEEP+NOTE"


def parse_llm_review_payload(
    data: dict[str, Any],
    *,
    top: list[ScoreRecord],
) -> dict[str, Any]:
    """Validate/normalize LLM JSON against primary board ids."""
    primary_ids = [m.molecule_id for m in top]
    allowed = set(primary_ids)
    seats_in = data.get("seats") if isinstance(data.get("seats"), list) else []
    by_id: dict[str, dict[str, Any]] = {}
    for row in seats_in:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("molecule_id") or "").strip()
        if mid not in allowed:
            continue
        decision = _normalize_decision(str(row.get("decision") or ""))
        severity = str(row.get("severity") or "medium").lower()
        if severity not in {"low", "medium", "high"}:
            severity = "high" if decision == "DROP" else "medium"
        issues = row.get("issue_types") if isinstance(row.get("issue_types"), list) else []
        by_id[mid] = {
            "rank": int(row.get("rank") or 0) or (primary_ids.index(mid) + 1),
            "molecule_id": mid,
            "identity_label": str(row.get("identity_label") or "未命名")[:40],
            "decision": decision,
            "rationale": str(row.get("rationale") or "").strip()[:120],
            "issue_types": [str(x)[:40] for x in issues[:4]],
            "severity": severity if decision != "DROP" else "high",
        }

    seats: list[dict[str, Any]] = []
    for i, mid in enumerate(primary_ids, start=1):
        if mid in by_id:
            row = by_id[mid]
            row["rank"] = i
            seats.append(row)
        else:
            seats.append(
                {
                    "rank": i,
                    "molecule_id": mid,
                    "identity_label": "未命名",
                    "decision": "KEEP+NOTE",
                    "rationale": "模型未返回该席位；请人工核对审计字段后决定。",
                    "issue_types": ["other"],
                    "severity": "medium",
                }
            )

    summary_in = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    keep_n = sum(1 for s in seats if s["decision"] == "KEEP")
    note_n = sum(1 for s in seats if s["decision"] == "KEEP+NOTE")
    drop_n = sum(1 for s in seats if s["decision"] == "DROP")
    extra = summary_in.get("extra_notes") if isinstance(summary_in.get("extra_notes"), list) else []
    conclusion = str(data.get("conclusion") or "").strip()
    if not conclusion:
        drops = [s["molecule_id"] for s in seats if s["decision"] == "DROP"]
        conclusion = (
            f"可作为代理提名工作短名单"
            + (f"，建议移出 {', '.join(drops)}。" if drops else "。")
        )
    intro = str(data.get("intro") or "").strip() or "已按身份/EPA/毒性/药史/结构警示核对。"
    return {
        "conclusion": conclusion[:100],
        "intro": intro[:120],
        "seats": seats,
        "summary": {
            "keep": keep_n,
            "keep_note": note_n,
            "drop": drop_n,
            "extra_notes": [str(x)[:80] for x in extra[:3]],
        },
    }


def apply_curated_decision_overrides(
    parsed: dict[str, Any],
    top: list[ScoreRecord],
) -> dict[str, Any]:
    """Force DROP when clinical/nomination curated hints require it."""
    hints = _curated_hints_for_board(top)
    if not hints:
        return parsed
    seats = list(parsed.get("seats") or [])
    changed = False
    for seat in seats:
        hint = hints.get(str(seat.get("molecule_id") or ""))
        if not hint or hint.get("preferred_decision") != "DROP":
            continue
        if seat.get("decision") != "DROP":
            changed = True
        seat["decision"] = "DROP"
        seat["severity"] = "high"
        issues = list(seat.get("issue_types") or [])
        if "drug_history" not in issues:
            issues = ["drug_history", *issues][:4]
        seat["issue_types"] = issues
        reason = str(hint.get("reason") or "").strip()
        if reason:
            seat["rationale"] = (reason.split("\n")[0].strip()[:100] or seat.get("rationale") or "")[:120]
        label = str(hint.get("label_hint") or "")
        if label and (
            not seat.get("identity_label")
            or str(seat.get("identity_label")).startswith("无公共名")
            or seat.get("identity_label") == "未命名"
        ):
            # Prefer a readable withdrawn-drug label when known.
            if "zomepirac" in reason.lower() or "zomepirac" in label.lower():
                seat["identity_label"] = "Zomepirac（撤市）"
            else:
                seat["identity_label"] = label.replace("_", " ")[:40]
    if changed:
        drops = [s["molecule_id"] for s in seats if s["decision"] == "DROP"]
        parsed["conclusion"] = (
            f"建议将 {', '.join(drops)} 移出主榜后再作为代理提名短名单。"
        )[:100]
        parsed["intro"] = "已合并仓库固化药史/临床排除提示；撤市硬风险优先 DROP。"[:120]
    parsed["seats"] = seats
    parsed["summary"] = {
        "keep": sum(1 for s in seats if s["decision"] == "KEEP"),
        "keep_note": sum(1 for s in seats if s["decision"] == "KEEP+NOTE"),
        "drop": sum(1 for s in seats if s["decision"] == "DROP"),
        "extra_notes": list((parsed.get("summary") or {}).get("extra_notes") or [])[:3],
    }
    return parsed


def seats_to_narrative_markdown(parsed: dict[str, Any]) -> str:
    lines = [
        f"**复核结论：** {parsed['conclusion']}",
        "",
        parsed["intro"],
        "",
        "### 逐席决定",
        "",
        "| 排名 | ID | 识别 | 决定 | 要点 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for seat in parsed["seats"]:
        rationale = str(seat["rationale"]).replace("|", "/").replace("\n", " ")
        label = str(seat["identity_label"]).replace("|", "/")
        lines.append(
            f"| {seat['rank']} | {seat['molecule_id']} | {label} | "
            f"{seat['decision']} | {rationale} |"
        )
    summary = parsed["summary"]
    lines.extend(
        [
            "",
            "### 汇总",
            "",
            f"- KEEP：{summary['keep']}",
            f"- KEEP+NOTE：{summary['keep_note']}",
            f"- DROP：{summary['drop']}",
        ]
    )
    for note in summary.get("extra_notes") or []:
        lines.append(f"- {note}")
    return "\n".join(lines)


def run_llm_nomination_review(
    top: list[ScoreRecord],
    reserve: list[ScoreRecord],
    *,
    llm_cfg: dict[str, Any] | None,
) -> dict[str, Any]:
    """Call LLM and return normalized review payload. Raises MechanismLLMError on failure."""
    settings = resolve_llm_settings(llm_cfg, purpose="nomination_review")
    # Seat narratives need more completion budget than short mechanism polish.
    if settings.max_tokens < 6144:
        settings = type(settings)(
            enabled=settings.enabled,
            model=settings.model,
            base_url=settings.base_url,
            api_key=settings.api_key,
            temperature=settings.temperature,
            timeout_sec=max(settings.timeout_sec, 90.0),
            max_tokens=8192,
            cache_dir=settings.cache_dir,
            use_cache=settings.use_cache,
        )
    pack = build_seat_evidence_pack(top, reserve)
    user = (
        "请根据以下审计证据包生成短清单复核 JSON（文案务必短）。\n\n"
        + json.dumps(pack, ensure_ascii=False, indent=2)
    )
    raw = chat_completion(settings, system=_SYSTEM, user=user)
    data = _extract_json_object(raw)
    parsed = parse_llm_review_payload(data, top=top)
    parsed = apply_curated_decision_overrides(parsed, top)
    parsed["narrative_markdown"] = seats_to_narrative_markdown(parsed)
    return parsed
