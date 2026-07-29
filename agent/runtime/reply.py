"""Agent 完成回复：亲和文案 + TopN 一览表 + 机制 PDF 简述。"""

from __future__ import annotations

import re
from typing import Any

from packages.models import ScoreRecord

# 药效团英文 → 中文（只取常见项，未知则保留原文短写）
_PHARMACOPHORE_ZH: dict[str, str] = {
    "carboxylic acid": "羧酸",
    "carboxylate": "羧酸盐",
    "phenoxy": "苯氧基",
    "aromatic ring": "芳香环",
    "secondary alcohol": "仲醇",
    "primary alcohol": "伯醇",
    "phenol": "酚羟基",
    "amide": "酰胺",
    "amine": "胺",
    "ester": "酯",
    "ketone": "酮",
    "ether": "醚",
    "halogen": "卤素",
    "sulfonamide": "磺酰胺",
    "urea": "脲",
}


def _clip(text: str, n: int) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(t) <= n:
        return t
    return t[: max(0, n - 1)] + "…"


def _fmt_score(value: float | None, *, digits: int = 3) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _pharmacophores(lipid_rationale: str) -> list[str]:
    m = re.search(r"药效团:\s*([^；;]+)", lipid_rationale or "")
    if not m:
        return []
    raw = [p.strip() for p in m.group(1).split(",") if p.strip()]
    out: list[str] = []
    for item in raw[:4]:
        key = item.lower()
        out.append(_PHARMACOPHORE_ZH.get(key, item))
    return out


def _tox_blurb(tox_risk: float) -> str:
    if tox_risk < 0.25:
        return "毒性风险偏低"
    if tox_risk < 0.40:
        return "毒性风险中等偏低"
    if tox_risk < 0.55:
        return "毒性风险中等"
    return "毒性风险偏高但仍过门控"


def why_nominated(mol: ScoreRecord) -> str:
    """一句话说明为何入选（面向对话，非公式展开）。"""
    bits: list[str] = []
    ph = _pharmacophores(mol.lipid_rationale or "")
    if ph:
        bits.append(f"降脂药效团含{'/'.join(ph)}")
    elif (mol.lipid_score or 0) >= 0.35:
        bits.append("降脂代理信号较好")
    else:
        bits.append("综合效果代理可入选")

    bits.append(_tox_blurb(float(mol.tox_risk or 0.0)))

    if float(mol.novelty_score or 0.0) >= 0.7:
        bits.append("相对参照有新颖空间")
    elif mol.selection_tier and mol.selection_tier not in {"score_only", ""}:
        bits.append(f"分层={mol.selection_tier}")

    return _clip("，".join(bits), 72)


def format_ranking_explanation(
    result: Any | None,
    *,
    molecule_id: str | None = None,
    rank_limit: int | None = None,
    rank_positions: tuple[int, ...] = (),
    rank_position_subject: bool = False,
) -> str | None:
    """Explain a frozen ranking without invoking or mutating ranking tools."""
    if result is None:
        return None

    top: list[ScoreRecord] = list(getattr(result, "top_molecules", None) or [])
    reserve: list[ScoreRecord] = list(
        getattr(result, "reserve_molecules", None) or []
    )
    scored: list[ScoreRecord] = list(
        getattr(result, "scored_molecules", None) or []
    )
    records: list[ScoreRecord] = []
    seen: set[str] = set()
    for mol in [*top, *reserve, *scored]:
        key = str(getattr(mol, "molecule_id", "") or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            records.append(mol)
    if not records:
        return None

    requested_positions: list[int] = []
    for raw_rank in rank_positions:
        try:
            rank = int(raw_rank)
        except (TypeError, ValueError):
            continue
        if rank > 0 and rank not in requested_positions:
            requested_positions.append(rank)

    # “介绍一下排名 Top5 的分子” names a single frozen record. Resolve that
    # record first so it cannot be turned into an overview of ranks 1–5.
    if (
        not molecule_id
        and rank_position_subject
        and len(requested_positions) == 1
        and requested_positions[0] <= len(top)
    ):
        molecule_id = str(top[requested_positions[0] - 1].molecule_id)

    # A single named position just outside the primary cutoff (for example
    # “Top11 为啥没有进榜” after a frozen Top10) refers to the corresponding
    # frozen reserve record. Explain the cutoff directly instead of falling
    # through to a Top10 overview or asking the LLM to guess.
    if not molecule_id and rank_position_subject and len(requested_positions) == 1:
        requested_rank = requested_positions[0]
        reserve_rank = requested_rank - len(top)
        if 1 <= reserve_rank <= len(reserve):
            target = reserve[reserve_rank - 1]
            run_id = str(getattr(result, "run_id", "") or "").strip()
            run_bit = f"（run `{run_id}`）" if run_id else ""
            target_score = (
                float(target.selection_score)
                if target.competition_scoring_version != "unassigned"
                else float(target.final_score)
            )
            cutoff = top[-1] if top else None
            comparison = ""
            if cutoff is not None:
                cutoff_score = (
                    float(cutoff.selection_score)
                    if cutoff.competition_scoring_version != "unassigned"
                    else float(cutoff.final_score)
                )
                comparison = (
                    f"主榜末位 Top {len(top)} `{cutoff.molecule_id}` 的组合排序分为 "
                    f"{_fmt_score(cutoff_score)}；该候补为 {_fmt_score(target_score)}。"
                )
            return (
                f"`{target.molecule_id}` 是上一轮冻结结果{run_bit}中的整体 Top {requested_rank}，"
                f"对应候补第 {reserve_rank} 位。它没有进入主榜的直接原因是本轮主榜名额"
                f"固定为 Top {len(top)}，并不是记录丢失。\n\n"
                f"{comparison}\n\n"
                f"冻结记录给出的候选理由是：{why_nominated(target)}。排序还会受到资格与风险"
                "硬门控及骨架/相似性多样性约束，因此不能只用某一个分项解释名次。"
                "这次只读取冻结结果，不会重新筛选、导出或修改排名。"
            )
        if requested_rank > len(top) + len(reserve):
            run_id = str(getattr(result, "run_id", "") or "").strip()
            run_bit = f"（run `{run_id}`）" if run_id else ""
            return (
                f"上一轮冻结结果{run_bit}只保存了 Top {len(top)} 主榜和 "
                f"{len(reserve)} 个候补，无法从冻结证据定位整体 Top {requested_rank}。"
                "我不会根据聊天上下文编造其分子或落选原因。"
            )

    # A request such as “解释 Top4 和 Top5” names two ranking subjects.  Do
    # not reinterpret its first number as “show the first four rows”: present
    # the two actual frozen records and the small score difference between
    # them, without mutating the frozen result.
    if not molecule_id and len(requested_positions) > 1:
        available = len(top)
        selected = [
            (rank, top[rank - 1])
            for rank in requested_positions
            if rank <= available
        ]
        missing = [rank for rank in requested_positions if rank > available]
        if not selected:
            return None

        run_id = str(getattr(result, "run_id", "") or "").strip()
        run_bit = f"（run `{run_id}`）" if run_id else ""
        rows = [
            "| 名次 | 分子 | 组合排序分 | 降脂代理 | 毒性风险 | 新颖性 |",
            "| ---: | --- | ---: | ---: | ---: | ---: |",
        ]
        detail_lines: list[str] = []
        for rank, mol in selected:
            score = (
                float(mol.selection_score)
                if mol.competition_scoring_version != "unassigned"
                else float(mol.final_score)
            )
            rows.append(
                "| "
                + " | ".join(
                    [
                        f"Top {rank}",
                        _md_cell(mol.molecule_id),
                        _fmt_score(score),
                        _fmt_score(mol.lipid_score),
                        _fmt_score(mol.tox_risk),
                        _fmt_score(mol.novelty_score),
                    ]
                )
                + " |"
            )
            detail_lines.append(
                f"- Top {rank} `{mol.molecule_id}`：{why_nominated(mol)}。"
            )

        comparison = ""
        if len(selected) >= 2:
            first_rank, first = selected[0]
            second_rank, second = selected[1]
            first_score = (
                float(first.selection_score)
                if first.competition_scoring_version != "unassigned"
                else float(first.final_score)
            )
            second_score = (
                float(second.selection_score)
                if second.competition_scoring_version != "unassigned"
                else float(second.final_score)
            )
            comparison = (
                f"Top {first_rank} 与 Top {second_rank} 的组合排序分相差 "
                f"{_fmt_score(abs(first_score - second_score))}"
                f"（{_fmt_score(first_score)} vs {_fmt_score(second_score)}）。"
            )

        missing_note = (
            "；" + "、".join(f"Top {rank}" for rank in missing) + "不在这轮冻结主榜中"
            if missing
            else ""
        )
        rank_label = "、".join(f"Top {rank}" for rank in requested_positions)
        rows_text = "\n".join(rows)
        details_text = "\n".join(detail_lines)
        return (
            f"上一轮冻结结果{run_bit}中，你点名的是"
            f"{rank_label}"
            f"{missing_note}。这次只读取冻结结果，不会重新筛选、导出或修改排名。\n\n"
            f"{rows_text}\n\n"
            f"{details_text}\n\n"
            f"{comparison}排序先经过资格与风险硬门控，再综合相对效应、新颖性和"
            "骨架/相似性多样性约束；因此不能把单一分项当成唯一决定因素。"
            "这些是计算优先级，仍需按验证方案进行实验确认。"
        )

    # “解释 Top15” is a request about the frozen shortlist as a whole, not an
    # instruction to make a new Top15 (and not an alias for explaining Top1).
    # Keep this path read-only and make the actual frozen count explicit so a
    # stale earlier Top10 turn cannot leak into the answer.
    requested_limit = int(rank_limit or 0)
    if not molecule_id and requested_limit > 1:
        available = len(top)
        shown = min(requested_limit, available)
        run_id = str(getattr(result, "run_id", "") or "").strip()
        run_bit = f"（run `{run_id}`）" if run_id else ""
        if available == 0:
            return None
        count_note = (
            f"这轮实际冻结了 Top {available}；"
            if requested_limit != available
            else ""
        )
        return (
            f"上一轮冻结结果{run_bit}{count_note}以下是其中 Top {shown} 的排名概览。"
            "这次只读取冻结结果，不会重新筛选、导出或修改排名。\n\n"
            f"{nomination_markdown_table(top[:shown])}\n\n"
            "排序先经过资格与风险硬门控，再综合相对效应、新颖性和骨架/相似性多样性约束。"
            "这些是计算优先级，仍需按验证方案进行实验确认。"
        )

    target: ScoreRecord | None = None
    wanted = str(molecule_id or "").strip().lower()
    if wanted:
        target = next(
            (
                mol
                for mol in records
                if str(getattr(mol, "molecule_id", "") or "").strip().lower()
                == wanted
            ),
            None,
        )
    elif top:
        target = top[0]
    if target is None:
        return None

    target_id = str(target.molecule_id)
    top_rank = next(
        (
            index
            for index, mol in enumerate(top, start=1)
            if str(mol.molecule_id).lower() == target_id.lower()
        ),
        None,
    )
    run_id = str(getattr(result, "run_id", "") or "").strip()
    run_bit = f"（run `{run_id}`）" if run_id else ""
    if top_rank is not None:
        opening = (
            f"{target_id} 已经进入上一轮冻结主榜{run_bit}，位于 Top {top_rank}。"
            "这次只解释现有名次，不会重新筛选或生成新排名。"
        )
    else:
        opening = (
            f"{target_id} 能在上一轮冻结结果{run_bit}中找到，"
            "这次只解释已有结果，不会重新筛选。"
        )

    selection_score = (
        float(target.selection_score)
        if target.competition_scoring_version != "unassigned"
        else float(target.final_score)
    )
    score_bits = [
        f"组合排序分 {_fmt_score(selection_score)}",
        f"降脂代理 {_fmt_score(target.lipid_score)}",
        f"毒性风险 {_fmt_score(target.tox_risk)}（越低越有利于通过风险门控）",
        f"新颖性 {_fmt_score(target.novelty_score)}",
    ]
    if target.effect_rank is not None:
        score_bits.append(f"效应相对名次 {target.effect_rank}")
    if target.novelty_rank is not None:
        score_bits.append(f"新颖性相对名次 {target.novelty_rank}")

    if target.competition_scoring_version != "unassigned":
        logic = (
            "它先通过资格与风险硬门控，再按同一运行内的相对效应代理和"
            "相对新颖性代理计算组合优先级，随后接受骨架/相似性多样性约束。"
        )
    else:
        logic = (
            "它先通过资格与风险硬门控，再按本轮综合分排序，"
            "随后接受骨架/相似性多样性约束。"
        )

    comparison = ""
    if top_rank == 1 and len(top) > 1:
        runner_up = top[1]
        runner_up_score = (
            float(runner_up.selection_score)
            if runner_up.competition_scoring_version != "unassigned"
            else float(runner_up.final_score)
        )
        comparison = (
            f" 在最终名单里，第二名 {runner_up.molecule_id} 的对应排序分为 "
            f"{_fmt_score(runner_up_score)}。"
        )

    rationale = why_nominated(target)
    position_conclusion = (
        f"所以 Top {top_rank} 表示它在这套配置和这批输入中的计算优先级位置，"
        if top_rank is not None
        else "所以这个位置只表示它在这套配置和这批输入中的计算优先级，"
    )
    return (
        f"{opening}\n\n"
        f"关键审计值是：{'；'.join(score_bits)}。{comparison}\n\n"
        f"{logic}当前记录给出的简要入选理由是：{rationale}。\n\n"
        f"{position_conclusion}"
        "不等于已经证实药效最好或最安全；仍需按报告中的双终点实验验证。"
    )


def _md_cell(text: str) -> str:
    return str(text or "").replace("|", "/").replace("\n", " ").strip()


def nomination_markdown_table(
    molecules: list[ScoreRecord], *, limit: int | None = None
) -> str:
    rows = list(molecules or [])
    if limit is not None:
        rows = rows[: max(1, int(limit))]
    lines = [
        "| 排名 | 分子 | 综合分 | 降脂 | 毒性风险 | 入选理由 |",
        "| ---: | --- | ---: | ---: | ---: | --- |",
    ]
    for i, mol in enumerate(rows, start=1):
        score = mol.selection_score if mol.selection_score else mol.final_score
        lines.append(
            "| "
            + " | ".join(
                [
                    str(i),
                    _md_cell(mol.molecule_id),
                    _fmt_score(score),
                    _fmt_score(mol.lipid_score),
                    _fmt_score(mol.tox_risk),
                    _md_cell(why_nominated(mol)),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def _pathway_hint(mol: ScoreRecord) -> str | None:
    """轻量通路提示：优先用已有机制图，避免在回复路径强依赖指纹推断。"""
    # selection_factors / overall_reason 里偶发 pathway=；没有则跳过
    factors = mol.selection_factors or {}
    for key in ("pathway", "pathway_id", "family"):
        val = str(factors.get(key) or "").strip()
        if val and val.lower() not in {"none", "n/a", "unresolved"}:
            return val
    text = f"{mol.selection_reason or ''} {mol.overall_reason or ''}"
    m = re.search(r"pathway[=:\s]+([A-Za-z0-9_\-]+)", text, re.I)
    if m:
        return m.group(1)
    return None


def mechanism_pdf_blurb(molecules: list[ScoreRecord]) -> str:
    n = len(molecules or [])
    if n <= 0:
        return (
            "机制 PDF 已生成：为入选分子整理了可检验的机制假说、证据边界，"
            "以及建议的 HepG2-FFA（脂质读出 + CCK-8 活力）验证步骤。"
            "这是计算假说层，不是湿实验结论。"
        )

    pathways: list[str] = []
    for mol in molecules[:8]:
        hint = _pathway_hint(mol)
        if hint and hint not in pathways:
            pathways.append(hint)

    head = (
        f"机制 PDF 已为这 {n} 个入选分子各写了一小节："
        "可检验的通路假说、证据边界，以及建议的 HepG2-FFA 双终点验证步骤"
        f"（初筛约 {molecules[0].screening_concentration_um:g} μM）。"
    )
    if pathways:
        head += f"报告里会涉及的通路线索包括：{'、'.join(pathways[:4])}。"
    else:
        head += "若某分子通路尚未锁定，PDF 会标明「先做双终点，命中后再解析机制」。"
    head += "请把它当作验证蓝图，而不是已证实的药效/安全性结论。"
    return head


def format_run_completion(
    *,
    want_csv: bool,
    want_pdf: bool,
    want_reserve: bool = False,
    want_bundle: bool = False,
    want_catalog: bool = False,
    result: Any | None,
) -> str:
    """主流程跑完后的亲和总结（可含 markdown 表格）。"""
    molecules: list[ScoreRecord] = list(getattr(result, "top_molecules", None) or [])
    n = len(molecules)
    src = str(getattr(result, "source_filename", "") or "").strip()
    src_bit = f"「{src}」" if src else "你上传的化合物库"

    parts: list[str] = []

    if want_csv and n:
        parts.append(
            f"已经从{src_bit}里筛出 Top {n} 优先名单。"
            "下面是对话里的速览（完整分项与审计字段在 CSV 里）："
        )
        parts.append(nomination_markdown_table(molecules))
        parts.append(
            "入选逻辑是：过硬门控后，按降脂代理 × 低毒余量 × 新颖性等做相对排序，"
            "并尽量拉开骨架多样性——属于计算优先级，不是实验命中证明。"
        )
    elif want_csv:
        parts.append("候选 CSV 已导出；当前没有可展示的入选行，请打开下方文件查看。")

    if want_reserve:
        reserve = list(getattr(result, "reserve_molecules", None) or [])
        requested = int(getattr(getattr(result, "config", None), "reserve_n", 20) or 20)
        if len(reserve) < requested:
            parts.append(
                f"候补名单已导出 {len(reserve)} 个合格候选（冻结目标 {requested} 个）。"
                "数量不足时未临时重跑或补入不合格候选。"
            )
        else:
            parts.append(f"候补名单已导出 {len(reserve)} 个合格候选。")
        parts.append(
            "候补仅在候选清单中的分子不可采购、无法配制或身份复核失败时，"
            "按冻结 reserve_rank 顺序顺延；不参与候选清单并列排序。"
        )

    if want_pdf:
        parts.append(mechanism_pdf_blurb(molecules))

    if want_catalog:
        parts.append("另外已附上 Catalog 旁证（只作参考，不改主榜排名）。")

    if want_bundle:
        parts.append("结果归档包已包含候选清单、候补名单、运行血缘清单与本会话轨迹。")

    if want_bundle:
        parts.append("下方可下载结果归档包；其中候选清单与候补来自同一次冻结运行。")
    elif want_csv and want_reserve:
        parts.append("下方可下载候选分子清单与候补名单。")
    elif want_reserve:
        parts.append("下方可下载冻结候补名单 CSV。")
    elif want_csv and want_pdf:
        parts.append("下方可下载候选 CSV 与机制 PDF。想改 Top N、只重出某一份，或追问某个分子，直接说就行。")
    elif want_csv:
        parts.append("下方可下载候选 CSV。若还需要机制与验证方案 PDF，或想调整 Top N，随时告诉我。")
    elif want_pdf:
        parts.append("下方可下载机制 PDF。若要换一批候选或改 Top N，也可以继续说。")
    else:
        parts.append("本轮请求已处理完毕。若还需要候选清单或机制报告，用自然语言说明即可。")

    return "\n\n".join(p for p in parts if p)
