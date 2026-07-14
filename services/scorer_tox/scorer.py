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


def fuse_tox(heads: dict[str, float], weights: dict[str, float], boost: float) -> float:
    active_w = {k: w for k, w in weights.items() if heads.get(k, 0.0) > 0 or k == "physchem"}
    for optional in ("dili", "admet", "evidence", "alert"):
        if optional in active_w and heads.get(optional, 0.0) == 0.0 and optional != "alert":
            if optional != "alert":
                active_w.pop(optional, None)
    if heads.get("alert", 0.0) == 0.0:
        active_w.pop("alert", None)
    if "physchem" not in active_w:
        active_w["physchem"] = weights.get("physchem", 0.15)
    total_w = sum(active_w.values()) or 1.0
    r = sum(active_w[k] * heads.get(k, 0.0) for k in active_w) / total_w
    return clamp(min(1.0, r + boost))


def score_tox(
    record: MoleculeRecord,
    cfg: AppConfig,
    gold: GoldSet,
    evidence: EvidenceBundle,
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
            cfg.mark_degraded("dili_ml")
            cfg.mark_degraded("admet_ml")
        else:
            ml_pred = bundle.predict(mol)
            if not ml_pred.skipped:
                if ml_pred.dili > 0:
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
                    r_admet = max(r_admet, clamp(ml_pred.admet))
                    attrs.append(
                        Attribution(
                            "admet_ml",
                            ml_pred.admet_neighbor or ml_pred.reason,
                            value=ml_pred.admet,
                            evidence_id=f"ml:{ml_pred.reason}",
                        )
                    )
    else:
        cfg.mark_degraded("dili_ml")
        cfg.mark_degraded("admet_ml")

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
    r_tox = fuse_tox(heads, cfg.tox_fuse, boost)

    parts = [f"R_tox={r_tox:.3f}"]
    parts.append(
        "heads["
        + ", ".join(f"{k}={v:.3f}" for k, v in heads.items() if v > 0 or k == "physchem")
        + "]"
    )
    if boost > 0:
        parts.append(f"analog_boost={boost:.3f}")
    if alert_hits:
        parts.append(f"警示: {', '.join(alert_hits)}")
    if phys_notes:
        parts.append("physchem: " + ", ".join(phys_notes))

    return r_tox, heads, boost, attrs, "；".join(parts)
