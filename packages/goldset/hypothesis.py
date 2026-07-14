"""结构启发 → NAFLD 通路假设（选榜配额与机制模板共用；不改 S_final 公式）。"""

from __future__ import annotations

from typing import Any

from packages.chem_core import PATHWAY_PATTERNS, match_weighted, morgan_fp
from packages.goldset.loader import load_goldset, max_similarity
from packages.goldset.pathways import (
    infer_pathway_for_positive,
    load_nafld_pathways,
    pathway_by_id,
)
from rdkit import Chem


def ensure_fp_bits(smiles: str, fp_bits: Any) -> Any:
    if fp_bits is not None:
        return fp_bits
    if not smiles:
        return None
    rdmol = Chem.MolFromSmiles(smiles)
    if rdmol is None:
        return None
    return morgan_fp(rdmol)


def infer_hypothesis_pathway(
    smiles: str,
    fp_bits: Any = None,
) -> tuple[dict[str, Any], str]:
    """返回 (pathway_dict, support_text)。无强阳性相似时回退 FAO。"""
    table = load_nafld_pathways()
    gold = load_goldset()
    support = "规则默认: FAO/DNL 启发（无强阳性相似）"
    bits = ensure_fp_bits(smiles, fp_bits)

    if bits is not None and gold.positives:
        sim, name = max_similarity(bits, gold.positives)
        if name and sim >= 0.35:
            mapped = infer_pathway_for_positive(name, table)
            if mapped:
                return mapped, f"阳性相似 {name} (sim={sim:.2f}) -> {mapped['id']}"

    rdmol = Chem.MolFromSmiles(smiles) if smiles else None
    if rdmol is not None:
        _score, hits = match_weighted(rdmol, PATHWAY_PATTERNS)
        hit_l = " ".join(hits).lower()
        prefer = None
        if "ppar" in hit_l or "fibrate" in hit_l:
            prefer = "PPAR"
        elif "ampk" in hit_l or "biguanide" in hit_l:
            prefer = "AMPK"
        elif "fxr" in hit_l or "bile" in hit_l:
            prefer = "FXR"
        elif "statin" in hit_l or "hmg" in hit_l or "dnls" in hit_l:
            prefer = "DNL"
        if prefer:
            mapped = pathway_by_id(prefer, table)
            if mapped:
                return mapped, f"结构启发: {', '.join(hits) or prefer}"

    fallback = pathway_by_id("FAO", table) or {
        "id": "FAO",
        "name": "脂肪酸氧化",
        "targets": ["PPARalpha", "AMPK", "CPT1"],
        "rescue_hint": "可用 AMPK 抑制剂 Compound C 做救援；或检测 CPT1A / ACOX1",
    }
    return fallback, support


def family_tag(smiles: str, fp_bits: Any = None) -> tuple[str | None, float, str | None]:
    """化学家族标签（用于 Top 配额）。返回 (tag, pos_sim, pos_name)。"""
    bits = ensure_fp_bits(smiles, fp_bits)
    gold = load_goldset()
    if bits is None or not gold.positives:
        return None, 0.0, None
    sim, name = max_similarity(bits, gold.positives)
    if not name:
        return None, sim, None
    # 家族门槛：足够像阳性才计入配额（避免弱相似误伤）
    if name == "Berberine" and sim >= 0.40:
        return "berberine", sim, name
    if name in {"Simvastatin", "Lovastatin"} and sim >= 0.40:
        return "statin_like", sim, name
    if name == "Metformin" and sim >= 0.40:
        return "biguanide", sim, name
    return None, sim, name
