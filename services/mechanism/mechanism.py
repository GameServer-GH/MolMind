"""机制与验证方案：准确模板生成（不改排名；默认不用 LLM 自由发挥）。

叙述结构：按假设通路分组 — 共用假说/验证协议，组内只列各候选差异。
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any

from packages.goldset.hypothesis import ensure_fp_bits, infer_hypothesis_pathway
from packages.models import ScoreRecord

# 通路展示顺序（白名单优先；未列出的按首次出现追加）
_PATHWAY_ORDER = (
    "AMPK",
    "PPAR",
    "FAO",
    "DNL",
    "FXR",
    "THR",
    "UptakeEfflux",
    "Autophagy",
)

# 全 ASCII 安全符号，避免 PDF CID 字体乱码
MEMBER_TEMPLATE = """### 候选 {rank}. {molecule_id}

- SMILES: `{smiles}`
- InChIKey: `{inchikey}`
- S_lipid={lipid_score:.3f}  R_tox={tox_risk:.3f}  S_final={final_score:.3f}
- 结构/对照启发: {pathway_support}
- 降脂: {lipid_rationale}
- 毒性: {tox_rationale}
- 综合: {overall_reason}
- 降脂相关证据 ID: {lipid_evidence}
- 毒理/警示证据 ID（不可当作靶点或通路证据）: {tox_evidence}
- 其他归因 ID: {other_evidence}

"""

SHARED_PROTOCOL = """## 统一实验验证协议（HepG2-FFA 双终点；各组共用）

1. 细胞模型: HepG2 + FFA（油酸/棕榈酸，常用约 2:1）诱导脂质蓄积；96 孔板。
2. 浓度: 建议 1 / 5 / 10 uM（按溶解性调整）；处理约 24 h；设溶剂对照。
3. 脂质终点: BODIPY 493/503 或 Nile Red（或等价读出）；相对模型/溶剂对照显著下降（建议 p < 0.05）。
4. 活力终点: CCK-8 / MTT / ATP 等；活力 >=80% 视为无明显细胞毒性干扰。
5. 有效命中（赛题口径）: 脂质显著下降 且 活力 >=80%（平行双终点；禁止仅凭脂滴下降判效）。
6. 可选确认阶段: 拟合 EC50、CC50，用 SI=CC50/EC50 看治疗窗（仅实验层；不进入 CSV 排序，ADR-M16）。
7. QC: DMSO 一致、荧光干扰对照、沉淀镜检、板级 Z'。
8. 风险与假阳性注意: 脂滴下降可能由细胞损伤引起；毒理/GHS 类证据只用于风险提示，不用于证明作用靶点。

"""


def _ascii_path_text(text: str) -> str:
    """靶点名等去掉易乱码希腊字母。"""
    return (
        (text or "")
        .replace("α", "alpha")
        .replace("β", "beta")
        .replace("γ", "gamma")
        .replace("δ", "delta")
        .replace("—", "-")
        .replace("–", "-")
        .replace("≥", ">=")
        .replace("≤", "<=")
        .replace("μ", "u")
        .replace("µ", "u")
        .replace("×", "x")
    )


def _split_evidence_ids(mol: ScoreRecord) -> tuple[list[str], list[str], list[str]]:
    """拆分：降脂相关 / 毒理警示 / 其他。GHS 不得进入降脂证据。"""
    lipid: list[str] = []
    tox: list[str] = []
    other: list[str] = []
    seen: set[str] = set()
    for attr in mol.attributions or []:
        eid = getattr(attr, "evidence_id", None)
        if not eid:
            continue
        eid_s = str(eid)
        if eid_s in seen:
            continue
        seen.add(eid_s)
        low = eid_s.lower()
        if "ghs" in low or ":tox" in low or "dili" in low:
            tox.append(eid_s)
        elif "lipid" in low or low.startswith("chembl:"):
            lipid.append(eid_s)
        elif low.startswith("pubchem:"):
            # 无 ghs 后缀的 pubchem 仍偏注释/毒理，放入毒理侧更稳妥
            tox.append(eid_s)
        elif low.startswith("ml:"):
            other.append(eid_s)
        else:
            other.append(eid_s)
    return lipid, tox, other


def _fmt_ids(ids: list[str], empty: str) -> str:
    return ", ".join(ids) if ids else empty


def _ensure_fp(mol: ScoreRecord) -> None:
    mol.fp_bits = ensure_fp_bits(mol.smiles, mol.fp_bits)


def _context(rank: int, mol: ScoreRecord) -> dict[str, Any]:
    _ensure_fp(mol)
    pathway, support = infer_hypothesis_pathway(mol.smiles, mol.fp_bits)
    targets = ", ".join(_ascii_path_text(str(t)) for t in (pathway.get("targets") or [])) or "待定"
    lipid_e, tox_e, other_e = _split_evidence_ids(mol)
    rescue = _ascii_path_text(
        str(pathway.get("rescue_hint") or "按假说设计救援或下游基因表达实验")
    )
    return {
        "rank": rank,
        "molecule_id": mol.molecule_id,
        "smiles": mol.smiles,
        "inchikey": mol.inchikey or "n/a",
        "lipid_score": mol.lipid_score,
        "tox_risk": mol.tox_risk,
        "final_score": mol.final_score,
        "lipid_rationale": _ascii_path_text(mol.lipid_rationale or ""),
        "tox_rationale": _ascii_path_text(mol.tox_rationale or ""),
        "overall_reason": _ascii_path_text(mol.overall_reason or ""),
        "pathway_id": pathway.get("id", "FAO"),
        "pathway_name": _ascii_path_text(str(pathway.get("name", "脂肪酸氧化"))),
        "targets": targets,
        "pathway_support": _ascii_path_text(support),
        "lipid_evidence": _fmt_ids(lipid_e, "无"),
        "tox_evidence": _fmt_ids(tox_e, "无"),
        "other_evidence": _fmt_ids(other_e, "无"),
        "rescue_hint": rescue,
    }


def _group_contexts(contexts: list[dict[str, Any]]) -> OrderedDict[str, list[dict[str, Any]]]:
    """按 pathway_id 分组，组内保持原排名顺序；组间按白名单优先。"""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for ctx in contexts:
        pid = str(ctx["pathway_id"])
        buckets.setdefault(pid, []).append(ctx)

    ordered: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for pid in _PATHWAY_ORDER:
        if pid in buckets:
            ordered[pid] = buckets.pop(pid)
    for pid, members in buckets.items():
        ordered[pid] = members
    return ordered


def _render_toc(groups: OrderedDict[str, list[dict[str, Any]]]) -> str:
    lines = ["## 通路分组一览\n\n"]
    for i, (pid, members) in enumerate(groups.items(), start=1):
        name = members[0]["pathway_name"]
        ids = ", ".join(f"{m['molecule_id']}(#{m['rank']})" for m in members)
        lines.append(f"{i}. `{pid}` - {name}（{len(members)}）：{ids}\n")
    lines.append("\n")
    return "".join(lines)


def _render_group(group_idx: int, members: list[dict[str, Any]]) -> str:
    head = members[0]
    pid = head["pathway_id"]
    default_n = sum(1 for m in members if "规则默认" in str(m["pathway_support"]))
    strength_note = (
        "本组含弱相似兜底启发（规则默认）；不声称已有强靶点/文献证据。"
        if default_n
        else "本组由阳性对照相似或结构启发映射到通路白名单；仍为计算启发，非湿实验结论。"
    )
    chunks = [
        f"## 通路组 {group_idx}. 假设通路 `{pid}` - {head['pathway_name']}\n\n",
        f"- 相关靶点: {head['targets']}\n",
        f"- 可选机制救援（本组共用）: {head['rescue_hint']}\n",
        f"- 说明: {strength_note} 不捏造文献。\n\n",
        "### 组内候选（差异信息）\n\n",
    ]
    for m in members:
        chunks.append(MEMBER_TEMPLATE.format(**m))
    chunks.append("---\n\n")
    return "".join(chunks)


def build_mechanism_markdown(
    top: list[ScoreRecord],
    *,
    llm_cfg: dict[str, Any] | None = None,
    mark_degraded: Any | None = None,
) -> str:
    """生成机制 Markdown。默认准确模板（按通路分组叙述；不用 LLM 自由撰写）。

    llm_cfg 保留兼容；mechanism_pdf=true 时也不再调用 LLM（准确性优先）。
    """
    del llm_cfg, mark_degraded  # 兼容旧调用签名；机制正文固定模板

    contexts = [_context(rank, mol) for rank, mol in enumerate(top, start=1)]
    groups = _group_contexts(contexts)

    chunks = [
        "# MolMind 机制与验证方案\n\n",
        f"共 {len(top)} 个候选；排名已冻结；生成模式：准确模板"
        "（按通路分组 + 通路白名单 + 归因拆分）。\n\n",
        "> 通路表: data/reference/nafld_pathways.yaml "
        "(DNL / FAO / PPAR / FXR / AMPK / THR / 摄取外排 / 自噬)\n\n",
        "> ADR-M16: EC50/CC50/SI/活力% 仅属实验验证协议，非计算排序输出。\n\n",
        "> 有效命中统一口径: 脂质显著下降 且 细胞活力 >=80%。"
        "不使用自拟的 20%/30% 脂质降幅作为赛题硬标准。\n\n",
        "> 叙述结构: 先按假设通路分组（共用假说与救援读出），"
        "再列组内各候选的计算层依据；统一实验协议只写一次。\n\n",
        _render_toc(groups),
        SHARED_PROTOCOL,
    ]
    for i, (_pid, members) in enumerate(groups.items(), start=1):
        chunks.append(_render_group(i, members))
    chunks.append(
        f"\n<!-- mechanism_stats llm=0 template={len(top)} "
        f"pathway_groups={len(groups)} degraded=none -->\n"
    )
    return "".join(chunks)


def render_mechanism_markdown(
    top: list[ScoreRecord],
    output_path: Path,
    *,
    llm_cfg: dict[str, Any] | None = None,
    mark_degraded: Any | None = None,
    write_pdf: bool = True,
) -> Path:
    """写入机制 Markdown；默认同时写入同名 .pdf。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    text = build_mechanism_markdown(
        top, llm_cfg=llm_cfg, mark_degraded=mark_degraded
    )
    output_path.write_text(text, encoding="utf-8")
    if write_pdf:
        from services.mechanism.pdf_export import markdown_to_pdf_bytes

        pdf_path = output_path.with_suffix(".pdf")
        pdf_path.write_bytes(markdown_to_pdf_bytes(text))
    return output_path
