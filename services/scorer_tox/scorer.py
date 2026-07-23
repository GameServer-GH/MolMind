"""多维毒性融合：禁常数 tox_risk。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from rdkit import Chem

from packages.chem_core import TOX_PATTERNS, clamp, match_weighted, physchem_risk
from packages.goldset import GoldSet, max_similarity
from packages.ml_optional import load_optional_heads_bundle
from packages.ml_optional.heads import OptionalHeadsBundle
from packages.models import Attribution, MoleculeRecord
from services.evidence_facade.bundle import EvidenceBundle
from services.pipeline.config_loader import ROOT, AppConfig


@lru_cache(maxsize=4)
def _heads_bundle(manifest_key: str, model_dir: str) -> OptionalHeadsBundle:
    import json

    manifest = json.loads(manifest_key)
    return load_optional_heads_bundle(manifest, model_dir=Path(model_dir))


def _get_ml_bundle(cfg: AppConfig) -> OptionalHeadsBundle:
    import json

    key = json.dumps(cfg.model_manifest, sort_keys=True, ensure_ascii=False)
    return _heads_bundle(key, str(ROOT))


def fuse_tox(heads: dict[str, float], weights: dict[str, object], boost: float) -> float:
    numeric_weights = {
        k: float(v) for k, v in weights.items() if k != "aggregation" and isinstance(v, (int, float))
    }
    active_w = {
        k: w for k, w in numeric_weights.items() if heads.get(k, 0.0) > 0 or k == "physchem"
    }
    for optional in ("dili", "admet", "evidence", "alert"):
        if optional in active_w and heads.get(optional, 0.0) == 0.0 and optional != "alert":
            if optional != "alert":
                active_w.pop(optional, None)
    if heads.get("alert", 0.0) == 0.0:
        active_w.pop("alert", None)
    if "physchem" not in active_w:
        active_w["physchem"] = numeric_weights.get("physchem", 0.15)
    total_w = sum(active_w.values()) or 1.0
    weighted_context = sum(active_w[k] * heads.get(k, 0.0) for k in active_w) / total_w
    # 风险融合必须单调：新增一个高风险头不得把已有风险稀释掉。
    max_head = max((heads.get(k, 0.0) for k in numeric_weights), default=0.0)
    return clamp(min(1.0, max(weighted_context, max_head) + boost))


def score_tox(
    record: MoleculeRecord,
    cfg: AppConfig,
    gold: GoldSet,
    evidence: EvidenceBundle,
    *,
    excluded_reference_names: set[str] | None = None,
) -> tuple[float, dict[str, float], float, list[Attribution], str]:
    mol = Chem.MolFromSmiles(record.smiles)
    attrs: list[Attribution] = []
    if mol is None:
        heads = {"alert": 1.0, "physchem": 1.0, "dili": 0.0, "admet": 0.0, "evidence": 0.0}
        return 1.0, heads, 0.0, [Attribution("error", "SMILES 无效")], "SMILES 无效，毒性置最高"

    alert_raw, alert_hits = match_weighted(mol, TOX_PATTERNS)
    r_alert = clamp(alert_raw)
    for h in alert_hits:
        attrs.append(Attribution("alert", h))

    r_physchem, phys_notes = physchem_risk(
        record.mw, record.logp, record.tpsa, record.aromatic_rings
    )
    for note in phys_notes:
        attrs.append(Attribution("physchem", note, value=r_physchem))

    r_dili = 0.0
    r_admet = 0.0
    dili_hits = [h for h in evidence.tox if h.adapter_id == "dili_table_v1"]
    other_tox = [h for h in evidence.tox if h.adapter_id != "dili_table_v1"]
    if dili_hits:
        r_dili = clamp(max(h.score for h in dili_hits))
        for hit in dili_hits:
            attrs.append(
                Attribution(
                    "dili_table",
                    hit.payload.get("name") or hit.adapter_id,
                    value=hit.score,
                    evidence_id=hit.evidence_id,
                )
            )

    ml_pred = None
    if cfg.ml_enabled:
        bundle = _get_ml_bundle(cfg)
        if bundle.skipped:
            # P0-C：缺模型 → *_missing（勿再用笼统 dili_ml）
            cfg.mark_degraded("dili_ml_missing")
            cfg.mark_degraded("admet_ml_missing")
        else:
            ml_pred = bundle.predict(
                mol,
                exclude_names=excluded_reference_names,
                exclude_similarity_at_or_above=0.98 if excluded_reference_names else None,
            )
            if not ml_pred.skipped:
                had_neighbor = False
                if ml_pred.dili > 0:
                    had_neighbor = True
                    r_dili = max(r_dili, clamp(ml_pred.dili))
                    attrs.append(
                        Attribution(
                            "dili_ml",
                            ml_pred.dili_neighbor or ml_pred.reason,
                            value=ml_pred.dili,
                            evidence_id=f"ml:{ml_pred.reason}",
                        )
                    )
                if ml_pred.admet > 0:
                    had_neighbor = True
                    r_admet = max(r_admet, clamp(ml_pred.admet))
                    attrs.append(
                        Attribution(
                            "admet_ml",
                            ml_pred.admet_neighbor or ml_pred.reason,
                            value=ml_pred.admet,
                            evidence_id=f"ml:{ml_pred.reason}",
                        )
                    )
                cfg.note_ml_predict(had_neighbor=had_neighbor)
    else:
        cfg.mark_degraded("dili_ml_missing")
        cfg.mark_degraded("admet_ml_missing")

    r_evidence = clamp(max((h.score for h in other_tox), default=0.0))
    for hit in other_tox:
        attrs.append(
            Attribution("evidence", hit.adapter_id, value=hit.score, evidence_id=hit.evidence_id)
        )

    fp_sim, fp_name = max_similarity(record.fp_bits, gold.false_positives)
    threshold = float(cfg.evidence.get("tox_analog_sim_threshold", 0.75))
    alpha = float(cfg.evidence.get("tox_analog_boost_alpha", 0.25))
    boost = 0.0
    if fp_sim >= threshold:
        boost = alpha * fp_sim
        attrs.append(
            Attribution(
                "tox_analog_boost",
                f"vs {fp_name}",
                value=round(fp_sim, 4),
            )
        )

    neg_sim, neg_name = max_similarity(record.fp_bits, gold.negatives)
    if neg_sim >= threshold and (neg_name or ""):
        boost = max(boost, 0.5 * alpha * neg_sim)
        attrs.append(Attribution("neg_analog_boost", f"vs {neg_name}", value=round(neg_sim, 4)))

    heads = {
        "alert": r_alert,
        "physchem": r_physchem,
        "dili": r_dili,
        "admet": r_admet,
        "evidence": r_evidence,
    }
    raw_risk = fuse_tox(heads, cfg.tox_fuse, boost)
    ml_similarity = 0.0
    if ml_pred is not None and not ml_pred.skipped:
        ml_similarity = max(float(ml_pred.dili_sim), float(ml_pred.admet_sim))

    # 覆盖度、风险信号可信度、安全结论可信度是三个不同概念。
    # 结构警示和理化性质是计算代理，不能伪装成“安全清除证据”。
    # 因此它们单独记录为 proxy_coverage，不进入 safety_clearance 或
    # toxicity_evidence_coverage；没有外部/实验安全证据时必须保持 audit_missing。
    local_proxy_coverage = clamp(float(cfg.gates.get("local_toxicity_proxy_coverage", 0.20)))
    model_applicability = clamp(ml_similarity)
    external_coverage = evidence.toxicity_evidence_coverage
    toxicity_evidence_coverage = max(
        model_applicability,
        external_coverage,
    )

    risk_signal_candidates = [local_proxy_coverage if r_physchem > 0 else 0.0]
    if alert_hits:
        risk_signal_candidates.append(0.40)
    if r_dili > 0 or r_admet > 0:
        risk_signal_candidates.append(clamp(0.45 + 0.45 * model_applicability))
    if evidence.tox:
        risk_signal_candidates.append(max(clamp(h.confidence) for h in evidence.tox))
    if fp_sim >= threshold or neg_sim >= threshold:
        risk_signal_candidates.append(clamp(0.45 + 0.45 * max(fp_sim, neg_sim)))
    risk_signal_confidence = clamp(max(risk_signal_candidates, default=0.0))

    # ML / 结构近邻低风险只算 proxy_clearance，不得伪装成外部安全清除证据。
    nomination_max = float(cfg.gates.get("tox_nomination_max", 0.45))
    proxy_clearance_candidates: list[float] = []
    if (
        model_applicability > 0
        and max(r_dili, r_admet) < nomination_max
        and not alert_hits
    ):
        proxy_clearance_candidates.append(clamp(0.45 + 0.45 * model_applicability))
    proxy_clearance_confidence = clamp(max(proxy_clearance_candidates, default=0.0))

    safety_clearance_candidates: list[float] = []
    for hit in evidence.tox:
        if (
            hit.evidence_role == "task_evidence"
            and hit.direction in {"supports_safety", "low_risk"}
            and hit.score < nomination_max
        ):
            safety_clearance_candidates.append(clamp(hit.confidence))
    safety_clearance_confidence = clamp(max(safety_clearance_candidates, default=0.0))

    # 兼容旧字段名：toxicity_confidence 现在明确等于安全结论可信度（外部证据）。
    tox_confidence = safety_clearance_confidence
    tox_uncertainty = 1.0 - toxicity_evidence_coverage
    uncertainty_penalty = float(cfg.gates.get("tox_uncertainty_penalty", 0.0)) * tox_uncertainty
    r_tox = clamp(raw_risk + uncertainty_penalty)
    min_confidence = float(cfg.gates.get("min_toxicity_confidence", 0.0))
    # 风险证据可以提高“发现危险”的覆盖度，却不能证明安全；因此是否使用
    # 保守提名边界只看 safety_clearance，而不能被 GHS/DILI 风险证据或 ML 近邻解除。
    low_safety_confidence = safety_clearance_confidence <= min_confidence + 1e-12
    low_confidence_margin = (
        float(cfg.gates.get("low_confidence_tox_margin", 0.0))
        if low_safety_confidence
        else 0.0
    )
    tox_upper_bound = clamp(r_tox + low_confidence_margin)
    heads["raw_risk"] = raw_risk
    heads["confidence"] = tox_confidence
    heads["uncertainty"] = tox_uncertainty
    heads["uncertainty_penalty"] = uncertainty_penalty
    heads["model_applicability"] = model_applicability
    heads["proxy_coverage"] = local_proxy_coverage
    heads["proxy_clearance_confidence"] = proxy_clearance_confidence
    heads["evidence_coverage"] = toxicity_evidence_coverage
    heads["risk_signal_confidence"] = risk_signal_confidence
    heads["safety_clearance_confidence"] = safety_clearance_confidence
    heads["tox_upper_bound"] = tox_upper_bound

    parts = [
        f"R_tox={r_tox:.3f}",
        f"raw={raw_risk:.3f}",
        f"tox_confidence={tox_confidence:.3f}",
        f"tox_coverage={toxicity_evidence_coverage:.3f}",
        f"risk_signal_confidence={risk_signal_confidence:.3f}",
        f"uncertainty_penalty={uncertainty_penalty:.3f}",
    ]
    parts.append(
        "heads["
        + ", ".join(
            f"{k}={v:.3f}"
            for k, v in heads.items()
            if k in {"alert", "physchem", "dili", "admet", "evidence"} and (v > 0 or k == "physchem")
        )
        + "]"
    )
    if low_confidence_margin > 0:
        parts.append(
            f"low_confidence_margin={low_confidence_margin:.3f}; "
            f"conservative_upper={tox_upper_bound:.3f}"
        )
    if boost > 0:
        parts.append(f"analog_boost={boost:.3f}")
    if alert_hits:
        parts.append(f"警示: {', '.join(alert_hits)}")
    epa_hits = [hit for hit in other_tox if hit.adapter_id == "epa_ctx_tox_v1"]
    if epa_hits:
        active_count = sum(
            int((hit.payload or {}).get("active_hit_count") or 0) for hit in epa_hits
        )
        nhit = max(
            (float((hit.payload or {}).get("nhit") or 0) for hit in epa_hits),
            default=0.0,
        )
        lower = next(
            (
                (hit.payload or {}).get("cytotox_lower_um")
                for hit in epa_hits
                if (hit.payload or {}).get("cytotox_lower_um") is not None
            ),
            None,
        )
        parts.append(
            f"epa_ctx_cytotox_strong nhit={nhit:g} lower_um={lower}; "
            f"active_assays={active_count}"
        )
    if phys_notes:
        parts.append("physchem: " + ", ".join(phys_notes))

    return r_tox, heads, boost, attrs, "；".join(parts)
