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

    if want_pdf:
        parts.append(mechanism_pdf_blurb(molecules))

    if want_catalog:
        parts.append("另外已附上 Catalog 旁证（只作参考，不改主榜排名）。")

    if want_csv and want_pdf:
        parts.append("下方可下载候选 CSV 与机制 PDF。想改 Top N、只重出某一份，或追问某个分子，直接说就行。")
    elif want_csv:
        parts.append("下方可下载候选 CSV。若还需要机制与验证方案 PDF，或想调整 Top N，随时告诉我。")
    elif want_pdf:
        parts.append("下方可下载机制 PDF。若要换一批候选或改 Top N，也可以继续说。")
    else:
        parts.append("本轮请求已处理完毕。若还需要候选清单或机制报告，用自然语言说明即可。")

    return "\n\n".join(p for p in parts if p)
