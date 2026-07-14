"""Critic：GoldSet 规则复查 + 家族/通路配额 + 证据约束 LLM Critic（默认关）。"""

from __future__ import annotations

from rdkit import Chem
from rdkit.Chem import Descriptors

from packages.goldset import GoldSet, max_similarity
from packages.goldset.hypothesis import family_tag, infer_hypothesis_pathway
from packages.models import CriticAction, ScoreRecord
from services.pipeline.config_loader import AppConfig


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
            "不利于效果×新颖性交卷"
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

    max_per_scaffold = int(cfg.diversity.get("max_per_scaffold", 2))
    max_family = _max_family(cfg)
    max_per_pathway = int(cfg.diversity.get("max_per_pathway", 0) or 0)

    for mol in final:
        scaf = mol.scaffold_smiles or mol.molecule_id
        scaffold_counts[scaf] = scaffold_counts.get(scaf, 0) + 1
        tag, _sim, _name = family_tag(mol.smiles, mol.fp_bits)
        if tag:
            family_counts[tag] = family_counts.get(tag, 0) + 1
        pid = _pathway_id(mol)
        pathway_counts[pid] = pathway_counts.get(pid, 0) + 1

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
    """交卷选榜：软踢 me-too/碎片 + 骨架/家族/通路配额，不足时放宽通路配额回填。"""
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
    ordered.sort(key=lambda m: (-m.final_score, m.molecule_id))

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

    return final[:top_n], actions


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
