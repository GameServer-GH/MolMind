"""Critic：GoldSet 规则复查 + 家族/通路配额 + 证据约束 LLM Critic（默认关）。"""

from __future__ import annotations

from rdkit import Chem
from rdkit.Chem import Descriptors

from packages.chem_core import tanimoto
from packages.goldset import GoldSet, max_similarity
from packages.goldset.hypothesis import family_tag, infer_hypothesis_pathway
from packages.models import CriticAction, ScoreRecord, format_selection_reason
from plugins.molmind_core.scientific.pipeline.config_loader import AppConfig
from plugins.molmind_core.scientific.ranker.ranker import competition_selection_score


def collect_run_evidence_ids(candidates: list[ScoreRecord]) -> set[str]:
    """工具：只读本 Run 候选上的 evidence_id（禁止引用未见 ID）。"""
    ids: set[str] = set()
    for mol in candidates:
        for attr in mol.attributions or []:
            eid = getattr(attr, "evidence_id", None)
            if eid:
                ids.add(str(eid))
    return ids


def filter_suggestions_by_run_evidence(
    suggestions: list[CriticAction],
    allowed_ids: set[str],
) -> tuple[list[CriticAction], list[CriticAction]]:
    """校验建议：drop/replace 必须带 evidence_ids，且全部 ⊆ 本 Run 证据集。

    返回 (accepted, rejected)。
    """
    accepted: list[CriticAction] = []
    rejected: list[CriticAction] = []
    for action in suggestions:
        if action.action == "keep":
            if action.evidence_ids and not set(action.evidence_ids).issubset(allowed_ids):
                rejected.append(action)
                continue
            accepted.append(action)
            continue
        if action.action in {"drop", "replace"}:
            if not action.evidence_ids:
                rejected.append(action)
                continue
            if not set(action.evidence_ids).issubset(allowed_ids):
                rejected.append(action)
                continue
            accepted.append(action)
            continue
        rejected.append(action)
    return accepted, rejected


def _mol_wt(smiles: str) -> float | None:
    if not smiles:
        return None
    rdmol = Chem.MolFromSmiles(smiles)
    if rdmol is None:
        return None
    return float(Descriptors.MolWt(rdmol))


def _named_soft_drop(cfg: AppConfig, pos_name: str | None) -> float | None:
    raw = cfg.critic.get("near_positive_soft_drop_named") or {}
    if not pos_name or not isinstance(raw, dict):
        return None
    if pos_name not in raw:
        return None
    return float(raw[pos_name])


def _should_drop(
    mol: ScoreRecord,
    cfg: AppConfig,
    gold: GoldSet,
) -> tuple[bool, str]:
    fp_thresh = float(cfg.critic.get("fp_sim_threshold", 0.75))
    tox_soft = float(cfg.critic.get("fp_tox_soft", cfg.gates.get("tox_soft", 0.45)))
    known_thresh = float(cfg.critic.get("known_positive_sim", 0.98))
    soft_drop = float(cfg.critic.get("near_positive_soft_drop", 0.78))
    min_novelty = float(cfg.critic.get("min_novelty_top", 0.0))
    min_mw = float(cfg.critic.get("min_mw_top", 0.0))

    pos_sim, pos_name = max_similarity(mol.fp_bits, gold.positives)
    if pos_sim >= known_thresh:
        return True, f"库内命中阳性对照本身 {pos_name}（sim={pos_sim:.3f}），非新发现提名"

    named = _named_soft_drop(cfg, pos_name)
    thresh = named if named is not None else soft_drop
    if pos_sim >= thresh:
        tag = f"named={pos_name}" if named is not None else "near_positive"
        return True, (
            f"过度近似阳性对照 {pos_name}（sim={pos_sim:.3f}≥{thresh:.2f}/{tag}），"
            "软降权移出 Top"
        )

    if min_novelty > 0 and mol.novelty_score < min_novelty:
        return True, (
            f"新颖性过低 novelty={mol.novelty_score:.3f}<{min_novelty:.2f}，"
            "不利于效力×新颖性组合"
        )

    if min_mw > 0:
        mw = _mol_wt(mol.smiles)
        if mw is not None and mw < min_mw:
            return True, f"分子量过低 MW={mw:.1f}<{min_mw:.0f}，疑似碎片/已知小分子，软移出 Top"

    fp_sim, fp_name = max_similarity(mol.fp_bits, gold.false_positives)
    if fp_sim >= fp_thresh and mol.tox_risk >= tox_soft:
        return True, f"与假阳性 {fp_name} 相似={fp_sim:.3f} 且 R_tox={mol.tox_risk:.3f}"
    return False, ""


def _max_family(cfg: AppConfig) -> dict[str, int]:
    raw = cfg.critic.get("max_family") or cfg.diversity.get("max_family") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): int(v) for k, v in raw.items()}


def _pathway_id(mol: ScoreRecord) -> str:
    pathway, _support = infer_hypothesis_pathway(mol.smiles, mol.fp_bits)
    return str(pathway.get("id") or "FAO")


def _select_with_quotas(
    ordered: list[ScoreRecord],
    cfg: AppConfig,
    gold: GoldSet,
    *,
    top_n: int,
    enforce_pathway: bool,
    seed: list[ScoreRecord] | None = None,
) -> tuple[list[ScoreRecord], list[CriticAction]]:
    """贪心选榜：软踢 → 骨架 → 家族 →（可选）通路配额。"""
    actions: list[CriticAction] = []
    final: list[ScoreRecord] = list(seed or [])
    scaffold_counts: dict[str, int] = {}
    family_counts: dict[str, int] = {}
    pathway_counts: dict[str, int] = {}
    cluster_counts: dict[str, int] = {}

    max_per_scaffold = int(cfg.diversity.get("max_per_scaffold", 2))
    max_family = _max_family(cfg)
    max_per_pathway = int(cfg.diversity.get("max_per_pathway", 0) or 0)
    max_pairwise = float(cfg.diversity.get("max_pairwise_tanimoto", 1.0))
    cluster_threshold = float(cfg.diversity.get("similarity_cluster_threshold", 1.0))
    max_per_cluster = int(cfg.diversity.get("max_per_similarity_cluster", 999999))

    for mol in final:
        scaf = mol.scaffold_smiles or mol.molecule_id
        scaffold_counts[scaf] = scaffold_counts.get(scaf, 0) + 1
        tag, _sim, _name = family_tag(mol.smiles, mol.fp_bits)
        if tag:
            family_counts[tag] = family_counts.get(tag, 0) + 1
        pid = _pathway_id(mol)
        pathway_counts[pid] = pathway_counts.get(pid, 0) + 1
        cluster = mol.similarity_cluster or mol.molecule_id
        cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1

    have = {m.molecule_id for m in final}
    for mol in ordered:
        if len(final) >= top_n:
            break
        if mol.molecule_id in have:
            continue
        drop, reason = _should_drop(mol, cfg, gold)
        if drop:
            actions.append(CriticAction(action="drop", molecule_id=mol.molecule_id, reason=reason))
            continue

        scaf = mol.scaffold_smiles or mol.molecule_id
        if scaffold_counts.get(scaf, 0) >= max_per_scaffold:
            actions.append(
                CriticAction(
                    action="drop",
                    molecule_id=mol.molecule_id,
                    reason=f"骨架超额 scaffold={scaf[:40]}",
                )
            )
            continue

        tag, _sim, _name = family_tag(mol.smiles, mol.fp_bits)
        if tag and tag in max_family and family_counts.get(tag, 0) >= max_family[tag]:
            actions.append(
                CriticAction(
                    action="drop",
                    molecule_id=mol.molecule_id,
                    reason=f"家族配额已满 family={tag} max={max_family[tag]}",
                )
            )
            continue

        similarities = [tanimoto(mol.fp_bits, prior.fp_bits) for prior in final]
        nearest = max(similarities, default=0.0)
        nearest_mol = final[similarities.index(nearest)] if similarities else None
        if nearest > max_pairwise:
            actions.append(
                CriticAction(
                    action="drop",
                    molecule_id=mol.molecule_id,
                    reason=(
                        f"候选内部相似度超额 nearest={getattr(nearest_mol, 'molecule_id', '')} "
                        f"sim={nearest:.3f}>{max_pairwise:.2f}"
                    ),
                )
            )
            continue
        cluster = mol.molecule_id
        if nearest_mol is not None and nearest >= cluster_threshold:
            cluster = nearest_mol.similarity_cluster or nearest_mol.molecule_id
        if cluster_counts.get(cluster, 0) >= max_per_cluster:
            actions.append(
                CriticAction(
                    action="drop",
                    molecule_id=mol.molecule_id,
                    reason=f"相似簇配额已满 cluster={cluster} max={max_per_cluster}",
                )
            )
            continue

        pid = _pathway_id(mol)
        if (
            enforce_pathway
            and max_per_pathway > 0
            and pathway_counts.get(pid, 0) >= max_per_pathway
        ):
            actions.append(
                CriticAction(
                    action="drop",
                    molecule_id=mol.molecule_id,
                    reason=f"通路假设配额已满 pathway={pid} max={max_per_pathway}",
                )
            )
            continue

        scaffold_counts[scaf] = scaffold_counts.get(scaf, 0) + 1
        if tag:
            family_counts[tag] = family_counts.get(tag, 0) + 1
        pathway_counts[pid] = pathway_counts.get(pid, 0) + 1
        cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
        mol.internal_nearest_similarity = round(nearest, 4)
        mol.similarity_cluster = cluster
        mol.selection_tier = "quota_strict" if enforce_pathway else "pathway_relaxed"
        mol.selection_factors = dict(mol.selection_factors or {})
        mol.selection_factors["eligibility"] = mol.eligibility_status
        mol.selection_factors["score"] = (
            f"competition={competition_selection_score(mol):.4f};"
            f"legacy={mol.final_score:.4f}"
        )
        mol.selection_factors["combo_adjustment"] = (
            f"{mol.selection_tier}; pathway={pid}; cluster={cluster}; "
            f"nearest_similarity={nearest:.3f}"
        )
        mol.selection_factors["evidence_coverage"] = mol.selection_factors.get(
            "evidence_coverage"
        ) or (
            f"lipid_conf={mol.conf_e:.3f};tox_coverage={mol.toxicity_evidence_coverage:.3f};"
            f"safety_clearance={mol.safety_clearance_confidence:.3f}"
        )
        mol.selection_reason = format_selection_reason(mol.selection_factors)
        note = f"规则 Critic 通过（pathway={pid}"
        if tag:
            note += f", family={tag}"
        note += ")"
        actions.append(CriticAction(action="keep", molecule_id=mol.molecule_id, reason=note))
        if tag or (enforce_pathway and max_per_pathway > 0):
            mol.overall_reason += f"；critic_quota pathway={pid}"
            if tag:
                mol.overall_reason += f" family={tag}"
        if not enforce_pathway:
            mol.overall_reason += "；pathway_quota_relaxed"
            note = f"通路配额放宽回填；{note}"
            actions[-1].reason = note
            mol.selection_factors["combo_adjustment"] += "; pathway_quota_relaxed"
            mol.selection_reason = format_selection_reason(mol.selection_factors)
        final.append(mol)
        have.add(mol.molecule_id)

    return final, actions


def rule_critic(
    top_k: list[ScoreRecord],
    pool: list[ScoreRecord],
    cfg: AppConfig,
    gold: GoldSet,
    *,
    top_n: int,
) -> tuple[list[ScoreRecord], list[CriticAction]]:
    """选榜：软踢 me-too/碎片 + 骨架/家族/通路配额，不足时放宽通路配额回填。"""
    seen: set[str] = set()
    ordered: list[ScoreRecord] = []
    top_ids = {m.molecule_id for m in top_k}
    for mol in list(top_k) + list(pool):
        if mol.molecule_id in seen:
            continue
        if mol.gated_out and mol.molecule_id not in top_ids:
            continue
        seen.add(mol.molecule_id)
        ordered.append(mol)
    ordered.sort(key=lambda m: (-competition_selection_score(m), m.molecule_id))

    final, actions = _select_with_quotas(
        ordered, cfg, gold, top_n=top_n, enforce_pathway=True
    )

    if len(final) < top_n:
        more, more_actions = _select_with_quotas(
            ordered,
            cfg,
            gold,
            top_n=top_n,
            enforce_pathway=False,
            seed=final,
        )
        final = more
        actions.extend(more_actions)

    final = final[:top_n]
    # 配额决定“是否入选”，不应覆盖入选后的分数顺序。旧实现把
    # pathway_relaxed 一律排到末尾，产生 rank 9 的分数高于 rank 5–8 的倒挂。
    final.sort(key=lambda mol: (-competition_selection_score(mol), mol.molecule_id))
    before_rank = {m.molecule_id: i for i, m in enumerate(ordered, start=1)}
    after_rank = {m.molecule_id: i for i, m in enumerate(final, start=1)}
    by_id = {m.molecule_id: m for m in ordered}
    checks = (
        "known_positive_similarity",
        "novelty_floor",
        "molecular_weight_floor",
        "false_positive_similarity",
        "toxicity_conflict",
        "scaffold_quota",
        "family_quota",
        "pathway_quota",
        "pairwise_similarity",
        "similarity_cluster_quota",
    )
    for action in actions:
        mol = by_id.get(action.molecule_id)
        if mol is None:
            continue
        action.original_status = "ranked_pool"
        action.checks_performed = checks
        action.score_before = mol.final_score
        action.score_after = mol.final_score
        action.eligibility_before = mol.eligibility_status
        action.eligibility_after = mol.eligibility_status
        action.rank_before = before_rank.get(mol.molecule_id)
        action.rank_after = after_rank.get(mol.molecule_id)
        action.final_decision = "selected" if mol.molecule_id in after_rank else "not_selected"
    return final, actions


def summarize_critic_actions(actions: list[CriticAction]) -> dict[str, int]:
    """P1-D：按 drop 原因粗分类计数（日志 / 诊断用）。"""
    buckets = {
        "known_positive": 0,
        "near_positive": 0,
        "fp_tox": 0,
        "novelty": 0,
        "mw": 0,
        "scaffold": 0,
        "family": 0,
        "pathway": 0,
        "keep": 0,
        "other_drop": 0,
    }
    for a in actions:
        if a.action == "keep":
            buckets["keep"] += 1
            continue
        if a.action != "drop":
            continue
        r = a.reason or ""
        if "阳性对照本身" in r or "库内命中" in r:
            buckets["known_positive"] += 1
        elif "过度近似阳性" in r or "near_positive" in r or "named=" in r:
            buckets["near_positive"] += 1
        elif "假阳性" in r:
            buckets["fp_tox"] += 1
        elif "新颖性过低" in r:
            buckets["novelty"] += 1
        elif "分子量过低" in r:
            buckets["mw"] += 1
        elif "骨架超额" in r:
            buckets["scaffold"] += 1
        elif "家族配额" in r:
            buckets["family"] += 1
        elif "通路" in r:
            buckets["pathway"] += 1
        else:
            buckets["other_drop"] += 1
    return buckets


def llm_critic_stub(
    candidates: list[ScoreRecord],
    cfg: AppConfig,
) -> list[CriticAction]:
    """可选 LLM Critic 桩：仅在开启时，对「已有证据」的分子发 keep（不改榜）。"""
    if not cfg.llm_critic_enabled:
        return []
    actions: list[CriticAction] = []
    for mol in candidates:
        evidence_ids = [
            str(a.evidence_id)
            for a in mol.attributions
            if getattr(a, "evidence_id", None)
        ]
        if not evidence_ids:
            continue
        actions.append(
            CriticAction(
                action="keep",
                molecule_id=mol.molecule_id,
                reason="llm_critic_stub: evidence-bound keep",
                evidence_ids=evidence_ids,
                original_status="critic_shortlist",
                checks_performed=("llm_evidence_binding",),
                eligibility_before=mol.eligibility_status,
                eligibility_after=mol.eligibility_status,
                final_decision="keep",
            )
        )
    return actions


def apply_llm_critic_suggestions(
    top: list[ScoreRecord],
    suggestions: list[CriticAction],
    *,
    affect_ranking: bool,
    allowed_evidence_ids: set[str] | None = None,
) -> list[ScoreRecord]:
    """仅接受带 evidence_ids 的 drop；若提供本 Run 允许集则须 ⊆ 该集合。"""
    if not affect_ranking:
        return top
    if allowed_evidence_ids is None:
        drop_ids = {
            a.molecule_id for a in suggestions if a.action == "drop" and a.evidence_ids
        }
    else:
        accepted, _rejected = filter_suggestions_by_run_evidence(
            suggestions, allowed_evidence_ids
        )
        drop_ids = {a.molecule_id for a in accepted if a.action == "drop" and a.evidence_ids}
    if not drop_ids:
        return top
    return [m for m in top if m.molecule_id not in drop_ids]


def run_evidence_bound_llm_critic(
    top: list[ScoreRecord],
    cfg: AppConfig,
) -> tuple[list[ScoreRecord], list[CriticAction]]:
    """编排：收集本 Run 证据 → stub/建议 → 校验 → 可选改榜。"""
    raw = llm_critic_stub(top, cfg)
    if not raw:
        return top, []
    allowed = collect_run_evidence_ids(top)
    accepted, rejected = filter_suggestions_by_run_evidence(raw, allowed)
    audit = list(accepted)
    for bad in rejected:
        audit.append(
            CriticAction(
                action="keep",
                molecule_id=bad.molecule_id,
                reason=f"llm_critic_rejected:{bad.action}:{bad.reason}",
                evidence_ids=list(bad.evidence_ids or []),
            )
        )
    affect = bool(cfg.raw.get("llm", {}).get("critic_affects_ranking", False))
    new_top = apply_llm_critic_suggestions(
        top, accepted, affect_ranking=affect, allowed_evidence_ids=allowed
    )
    return new_top, audit
