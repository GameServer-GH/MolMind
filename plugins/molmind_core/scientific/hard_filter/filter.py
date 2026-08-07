"""Hard Filter：Ro5 + 专家红线 + 警示结构。

HF contract lineage: YLuo / LJR
"""

from __future__ import annotations

from functools import lru_cache

from rdkit import Chem

from packages.models import FilterDecision, MoleculeRecord, StructuralAlertHit
from plugins.molmind_core.scientific.pipeline.config_loader import AppConfig


@lru_cache(maxsize=16)
def _compiled_structural_alerts(
    rules_key: tuple[tuple[str, str, str], ...],
) -> tuple[tuple[str, str, str, object | None], ...]:
    compiled: list[tuple[str, str, str, object | None]] = []
    for name, smarts, classification in rules_key:
        pattern = Chem.MolFromSmarts(smarts) if smarts else None
        compiled.append((name, smarts, classification, pattern))
    return tuple(compiled)


def _alert_rules(cfg: AppConfig) -> tuple[tuple[str, str, str, object | None], ...]:
    steps = cfg.filter_steps.get("steps", [])
    alert_step = next((s for s in steps if s.get("id") == "structural_alerts"), {})
    rules_key = tuple(
        (
            str(rule.get("id") or "unnamed_alert"),
            str(rule.get("smarts") or ""),
            str(rule.get("classification") or "review_required"),
        )
        for rule in alert_step.get("rules") or []
    )
    return _compiled_structural_alerts(rules_key)


def apply_hard_filters(record: MoleculeRecord, cfg: AppConfig) -> FilterDecision:
    steps = cfg.filter_steps.get("steps", [])
    step_codes: list[str] = []

    ro5 = next((s for s in steps if s.get("id") == "lipinski_ro5"), {})
    params = ro5.get("params", {})
    max_mw = float(params.get("max_mw", 500.0))
    max_logp = float(params.get("max_logp", 5.0))
    max_hbd = int(params.get("max_hbd", 5))
    max_hba = int(params.get("max_hba", 10))

    ro5_violations: list[tuple[str, str]] = []
    if record.mw > max_mw:
        ro5_violations.append(("ro5_mw", f"MW {record.mw:.1f} > {max_mw}"))
    if record.logp > max_logp:
        ro5_violations.append(("ro5_logp", f"LogP {record.logp:.2f} > {max_logp}"))
    if record.hbd > max_hbd:
        ro5_violations.append(("ro5_hbd", f"HBD {record.hbd} > {max_hbd}"))
    if record.hba > max_hba:
        ro5_violations.append(("ro5_hba", f"HBA {record.hba} > {max_hba}"))
    step_codes.append("lipinski_ro5")
    reason_codes = [code for code, _ in ro5_violations]
    reasons = [reason for _, reason in ro5_violations]
    status = "review_required" if ro5_violations else "passed"
    if ro5_violations and ro5.get("classification") == "hard_exclusion":
        return FilterDecision(
            False,
            step_codes,
            "; ".join(reasons),
            status="rejected",
            reason_codes=reason_codes,
        )

    red = next((s for s in steps if s.get("id") == "expert_redlines"), {})
    red_params = red.get("params", {})
    max_mw_hard = float(red_params.get("max_mw_hard", 600.0))
    max_logp_hard = float(red_params.get("max_logp_hard", 5.0))
    if record.mw > max_mw_hard:
        return FilterDecision(
            False,
            step_codes + ["expert_redlines"],
            f"红线 MW>{max_mw_hard}",
            status="rejected",
            reason_codes=reason_codes + ["hard_mw"],
        )
    if record.logp > max_logp_hard:
        return FilterDecision(
            False,
            step_codes + ["expert_redlines"],
            f"红线 LogP {record.logp:.2f} > {max_logp_hard}",
            status="rejected",
            reason_codes=reason_codes + ["hard_logp"],
        )
    step_codes.append("expert_redlines")

    mol = Chem.MolFromSmiles(record.smiles)
    if mol is None:
        return FilterDecision(
            False,
            step_codes + ["structural_alerts"],
            "SMILES 无法解析",
            status="invalid",
            reason_codes=reason_codes + ["invalid_smiles"],
        )

    alert_hits: list[StructuralAlertHit] = []
    for name, smarts, classification, pattern in _alert_rules(cfg):
        if pattern is not None and mol.HasSubstructMatch(pattern):
            hit = StructuralAlertHit(name, classification, smarts)
            alert_hits.append(hit)
            reason_codes.append(f"alert:{name}:{classification}")
            if classification == "hard_exclusion":
                reasons.append(f"硬排除结构警示: {name}")
                return FilterDecision(
                    False,
                    step_codes + ["structural_alerts"],
                    "; ".join(reasons),
                    status="rejected",
                    reason_codes=reason_codes,
                    alert_hits=alert_hits,
                )
            if classification == "review_required":
                status = "review_required"
                reasons.append(f"需复核结构警示: {name}")
            elif classification == "soft_penalty":
                reasons.append(f"软毒性警示: {name}")
            elif classification == "information_only":
                reasons.append(f"信息性结构提示: {name}")
    step_codes.append("structural_alerts")
    step_codes.append("basic_props")
    if not reasons:
        reasons.append("类药性复核与专家红线通过")
    return FilterDecision(
        True,
        step_codes,
        "; ".join(reasons),
        status=status,
        reason_codes=reason_codes,
        alert_hits=alert_hits,
    )
