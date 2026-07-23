"""机制与验证方案：准确模板生成（不改排名；默认不用 LLM 自由发挥）。

叙述结构：按假设通路分组 — 共用假说/验证协议，组内只列各候选差异。
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any

from rdkit import Chem
from rdkit.Chem import Crippen, Descriptors

from packages.goldset.hypothesis import ensure_fp_bits, infer_hypothesis_pathway
from packages.models import ScoreRecord
from services.evidence_facade.mechanism_graph import MechanismGraph

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
    "UNRESOLVED",
)

# 全 ASCII 安全符号，避免 PDF CID 字体乱码
MEMBER_TEMPLATE = """### 候选 {rank}. {molecule_id}

- SMILES: `{smiles}`
- InChIKey: `{inchikey}`
- S_lipid={lipid_score:.3f}  R_tox={tox_risk:.3f}  S_final={final_score:.3f}
- 资格: {eligibility_status}（仅表示项目代理门控通过；尚未经 HepG2-FFA 脂质/活力双终点实验证实）
- 机制证据等级: {mechanism_evidence_level} - {mechanism_evidence_type}
- 结构/对照启发: {pathway_support}
- 降脂: {lipid_rationale}
- 毒性: {tox_rationale}
- 综合: {overall_reason}
- 降脂相关证据 ID: {lipid_evidence}
- 毒理/警示证据 ID（不可当作靶点或通路证据）: {tox_evidence}
- EPA CTX/ToxCast 审计: {epa_audit}
- ChEMBL 查询: {chembl_audit}
- PubChem 查询: {pubchem_audit}
- BindingDB 机制支持: {bindingdb_audit}
- DILIrank exact 审计: {dili_audit}
- 其他归因 ID: {other_evidence}
- 候选特异验证: {candidate_validation}

"""

SHARED_PROTOCOL = """## 统一实验验证协议（HepG2-FFA 双终点；各组共用）

1. 科学目标: 仅在未明显损伤细胞活力的前提下降低脂质蓄积才计为有效命中；数值阈值、剂量、时长与 FFA 配比按实验室 SOP 确定。
2. 项目建议起始模型: HepG2 + FFA（油酸/棕榈酸约 2:1）诱导脂质蓄积；该配比需按实验室 SOP 与预实验调整。
3. 已确认初筛暴露固定为 10 uM；处理时长、FFA 配比等未确认细节按实验室 SOP 执行。
4. 脂质终点: 具体主读出与命中降幅尚未确认；不得把 BODIPY、Nile Red 或内部代理阈值写成官方标准。
5. 活力主终点: CCK-8；相对对照 >={viability_pct:.0f}% 为未明显损伤参考；Hoechst 为辅助读出。
6. 项目操作性判读: 脂质读出与 CCK-8 活力须在同一浓度/时长下平行解读；脂质达标与活力达标同时成立才可作为有效命中候选，具体脂质门槛待 SOP 确认。
7. 可选确认阶段: 拟合 EC50、CC50，用 SI=CC50/EC50 看治疗窗（仅实验层；不进入 CSV 排序，ADR-M16）。
8. QC: DMSO 一致、荧光干扰对照、沉淀镜检、板级 Z'。
9. 风险与假阳性注意: 脂滴下降可能由细胞损伤引起；毒理/GHS 类证据只用于风险提示，不用于证明作用靶点。

"""


def _viability_proxy(assumptions: dict[str, Any] | None) -> float:
    for row in (assumptions or {}).get("assumptions") or []:
        if isinstance(row, dict) and row.get("id") == "viability_proxy":
            return float(row.get("value", 0.80))
    return 0.80


def _candidate_validation_plan(mol: ScoreRecord, pathway_id: str) -> str:
    rdmol = Chem.MolFromSmiles(mol.smiles)
    risks: list[str] = []
    if rdmol is not None:
        logp = float(Crippen.MolLogP(rdmol))
        mw = float(Descriptors.MolWt(rdmol))
        if logp >= 4.0 or mw >= 500:
            risks.append("先做溶解度/沉淀/游离浓度检查")
        if Descriptors.NumAromaticRings(rdmol) >= 3:
            risks.append("增加无细胞荧光干扰与正交甘油三酯定量")
    if mol.risk_signal_confidence > 0.0 or mol.tox_heads.get("alert", 0.0) > 0.0:
        risks.append("并行实时细胞计数、LDH/ATP 与凋亡读出")
    if pathway_id == "UNRESOLVED":
        risks.append(
            "先确认脂质/活力双终点；命中后再用转录组/磷酸化信号、表型救援或靶点去卷积做无偏解析"
        )
    else:
        risks.append("按假说通路做下游基因、拮抗剂或敲低救援及时间顺序验证")
    return "；".join(risks or ["先做双终点剂量-反应，再按证据升级机制实验"])


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
        if "ghs" in low or ":tox" in low or "dili" in low or "epa_ctx" in low:
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
    support_l = support.lower()
    if support.startswith("阳性相似"):
        evidence_type = "相似已知分子（结构相似性推断；非靶点实测）"
        evidence_level = "L3"
    elif support.startswith("结构启发"):
        evidence_type = "结构推断（SMARTS 启发；非靶点实测）"
        evidence_level = "L4"
    elif "证据不足" in support_l or "证据不足" in support:
        evidence_type = "无候选级机制证据；待实验解析"
        evidence_level = "L5-UNRESOLVED"
    else:
        evidence_type = "模型/规则推断；待实验验证"
        evidence_level = "L4"
    if lipid_e:
        evidence_level = "L2"
        evidence_type = "同分子数据库任务证据；仍需 HepG2-FFA 条件复核"
    return {
        "rank": rank,
        "molecule_id": mol.molecule_id,
        "smiles": mol.smiles,
        "inchikey": mol.inchikey or "n/a",
        "lipid_score": mol.lipid_score,
        "tox_risk": mol.tox_risk,
        "final_score": mol.final_score,
        "eligibility_status": mol.eligibility_status,
        "mechanism_evidence_type": evidence_type,
        "mechanism_evidence_level": evidence_level,
        "lipid_rationale": _ascii_path_text(mol.lipid_rationale or ""),
        "tox_rationale": _ascii_path_text(mol.tox_rationale or ""),
        "overall_reason": _ascii_path_text(mol.overall_reason or ""),
        "pathway_id": pathway.get("id", "FAO"),
        "pathway_name": _ascii_path_text(str(pathway.get("name", "脂肪酸氧化"))),
        "targets": targets,
        "pathway_support": _ascii_path_text(support),
        "lipid_evidence": _fmt_ids(lipid_e, "无"),
        "tox_evidence": _fmt_ids(tox_e, "无"),
        "epa_audit": _ascii_path_text(
            "; ".join(
                f"{key}={value}"
                for key, value in sorted((mol.epa_audit or {}).items())
                if key
                in {
                    "stage",
                    "status",
                    "query_status",
                    "mapping_status",
                    "mapping_basis",
                    "matched_identity_type",
                    "dtxsid",
                    "active_hit_count",
                    "bioactivity_record_count",
                    "risk_applied",
                }
            )
            or "disabled"
        ),
        "chembl_audit": _ascii_path_text(
            "; ".join(
                f"{key}={value}"
                for key, value in sorted(
                    ((mol.evidence_source_audit or {}).get("chembl") or {}).items()
                )
                if key in {"status", "hit_count", "scored_hit_count", "ranking_effect"}
            )
            or "not_queried"
        ),
        "pubchem_audit": _ascii_path_text(
            "; ".join(
                f"{key}={value}"
                for key, value in sorted(
                    ((mol.evidence_source_audit or {}).get("pubchem") or {}).items()
                )
                if key in {"status", "hit_count", "scored_hit_count", "ranking_effect"}
            )
            or "not_queried"
        ),
        "bindingdb_audit": _ascii_path_text(
            "; ".join(
                f"{key}={value}"
                for key, value in sorted(
                    ((mol.evidence_source_audit or {}).get("bindingdb") or {}).items()
                )
                if key in {"status", "hit_count", "scored_hit_count", "ranking_effect"}
            )
            or "not_queried"
        ),
        "dili_audit": _ascii_path_text(
            "; ".join(
                f"{key}={value}"
                for key, value in sorted((mol.dili_audit or {}).items())
                if key
                in {
                    "status",
                    "action",
                    "concern",
                    "match_basis",
                    "compound_name",
                    "ltkb_id",
                }
            )
            or "disabled"
        ),
        "other_evidence": _fmt_ids(other_e, "无"),
        "rescue_hint": rescue,
        "candidate_validation": _candidate_validation_plan(mol, str(pathway.get("id", "UNRESOLVED"))),
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
    default_n = sum(1 for m in members if m["pathway_id"] == "UNRESOLVED")
    strength_note = (
        "本组缺少候选级通路证据；不指定 SREBP-1c、PPARalpha 或 AMPK 为既定机制。"
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
    assumptions: dict[str, Any] | None = None,
    run_context: dict[str, str] | None = None,
    mechanism_graphs: list[MechanismGraph] | None = None,
) -> str:
    """生成机制 Markdown。默认准确模板（按通路分组叙述；不用 LLM 自由撰写）。

    llm_cfg 保留兼容；mechanism_pdf=true 时也不再调用 LLM（准确性优先）。
    """
    del llm_cfg, mark_degraded  # 兼容旧调用签名；机制正文固定模板
    if any(m.eligibility_status != "eligible" or m.gated_out for m in top):
        raise ValueError("机制报告只能包含最终 eligibility_status=eligible 的候选")

    contexts = [_context(rank, mol) for rank, mol in enumerate(top, start=1)]
    groups = _group_contexts(contexts)
    viability_pct = 100.0 * _viability_proxy(assumptions)
    lineage = run_context or {}
    lineage_block = ""
    if lineage:
        lineage_block = (
            "> 运行绑定: "
            f"`run_id={lineage.get('run_id', '')}`；"
            f"`input_sha256={lineage.get('input_sha256', '')}`；"
            f"`config_hash={lineage.get('config_hash', '')}`；"
            f"`selection_sha256={lineage.get('selection_sha256', '')}`。\n\n"
        )
    graph_block = ""
    if mechanism_graphs:
        graph_chunks = [
            "## 分层机制证据图（非评分）\n\n",
            "> `candidate→target` 当前均为结构/相似性假说；Open Targets 与 Reactome 仅提供靶点背景。"
            "没有候选直接结合或扰动数据时，不得写成已证实机制。\n\n",
        ]
        for graph in mechanism_graphs:
            graph_chunks.append(
                f"### {graph.molecule_id}: {graph.target_symbol or 'UNRESOLVED'}\n\n"
                f"- 链状态: `{graph.chain_status}`\n"
                f"- 证据图快照 SHA-256: `{graph.context_snapshot_sha256 or 'none'}`\n"
            )
            for edge in graph.edges:
                graph_chunks.append(
                    f"- `{edge.source}` —{edge.relation}→ `{edge.target}`；"
                    f"等级 `{edge.evidence_level}`；直接性 `{edge.directness}`；"
                    f"证据 `{', '.join(edge.evidence_ids) or 'none'}`。\n"
                )
            if graph.evidence_gaps:
                graph_chunks.append(f"- 待补证: `{', '.join(graph.evidence_gaps)}`。\n")
            graph_chunks.append("\n")
        graph_block = "".join(graph_chunks)

    citation_chunks = [
        "## 候选级引用清单（非评分）\n\n",
        "> 分栏：`endpoint_evidence`=候选直接终点证据；`identity_annotation`=数据库身份注释；"
        "`mechanism_context`=靶点/通路背景；`query_audit`=查询状态。"
        "缺失 PMID/DOI 时保持空，禁止捏造。\n\n",
    ]
    for mol in top:
        citation_chunks.append(f"### {mol.molecule_id}\n\n")
        if not mol.citations:
            citation_chunks.append("- （无结构化引用；仅代理评分或查询未命中）\n\n")
            continue
        by_type: dict[str, list] = {}
        for cite in mol.citations:
            by_type.setdefault(cite.evidence_type or "unresolved", []).append(cite)
        for etype in (
            "endpoint_evidence",
            "identity_annotation",
            "mechanism_context",
            "query_audit",
            "unresolved",
        ):
            rows = by_type.get(etype) or []
            if not rows:
                continue
            citation_chunks.append(f"**{etype}**\n\n")
            for cite in rows:
                value_unit = ""
                if cite.value or cite.unit:
                    value_unit = f"；数值 `{cite.value} {cite.unit}`"
                citation_chunks.append(
                    f"- `{cite.evidence_id}`；来源 `{cite.source}`；accession `{cite.accession or 'none'}`；"
                    f"终点 `{cite.endpoint or 'none'}`；方向 `{cite.direction}`；"
                    f"实体 `{cite.matched_entity or 'none'}`；PMID/DOI `{cite.pmid_or_doi or 'none'}`；"
                    f"查询日 `{cite.queried_at or 'none'}`{value_unit}\n"
                )
            citation_chunks.append("\n")
    citation_block = "".join(citation_chunks)

    chunks = [
        "# MolMind 机制与验证方案\n\n",
        f"共 {len(top)} 个候选；排名已冻结；生成模式：准确模板"
        "（按通路分组 + 通路白名单 + 归因拆分）。\n\n",
        lineage_block,
        "> 通路表: data/reference/nafld_pathways.yaml "
        "(DNL / FAO / PPAR / FXR / AMPK / THR / 摄取外排 / 自噬)\n\n",
        "> 常见机制假说空间包括：从头脂合成（SREBP-1c / ACC / FASN / SCD1）、"
        "脂肪酸氧化（PPARalpha / AMPK / CPT1）、脂质摄取与外排、自噬。"
        "仅在候选级结构或相似性证据支持时分配具体通路；否则标记 UNRESOLVED。\n\n",
        "> ADR-M16: EC50/CC50/SI/活力% 仅属实验验证协议，非计算排序输出。\n\n",
        "> 资格语义: `eligible` 仅表示通过项目配置的降脂与毒性代理门控，"
        "不等同于已证实的 HepG2-FFA 有效低毒命中。最终有效性必须由平行脂质/活力实验确认。\n\n",
        f"> 候选级证据覆盖: 降脂证据命中 {sum(m.conf_e > 0 for m in top)}/{len(top)}；"
        f"任务模型适用域命中 {sum(m.toxicity_model_applicability > 0 for m in top)}/{len(top)}。"
        "零命中候选仅由结构、理化性质和相似性代理支持。\n\n",
        "> 阈值分层: 科学问题要求“未明显损伤细胞活力”；"
        f"项目暂以活力 >={viability_pct:.0f}% 作为操作性代理参考，非官方标准。"
        "不使用自拟的脂质降幅作为硬标准。\n\n",
        "> 机制证据等级: L1=同分子直接机制实验；L2=同分子任务/靶点证据；"
        "L3=高相似已知分子；L4=结构/规则推断；L5-UNRESOLVED=无候选级方向证据。\n\n",
        "> 叙述结构: 先按假设通路分组（共用假说与救援读出），"
        "再列组内各候选的计算层依据；统一实验协议只写一次。\n\n",
        _render_toc(groups),
        SHARED_PROTOCOL.format(viability_pct=viability_pct),
    ]
    for i, (_pid, members) in enumerate(groups.items(), start=1):
        chunks.append(_render_group(i, members))
    # 专家首先看到候选与验证协议；证据图和逐条引用作为后置审计附录，
    # 避免长串 provenance 字段把主叙事推到 PDF 前几页。
    chunks.extend([graph_block, citation_block])
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
    assumptions: dict[str, Any] | None = None,
    run_context: dict[str, str] | None = None,
    mechanism_graphs: list[MechanismGraph] | None = None,
) -> Path:
    """写入机制 Markdown；默认同时写入同名 .pdf。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    text = build_mechanism_markdown(
        top,
        llm_cfg=llm_cfg,
        mark_degraded=mark_degraded,
        assumptions=assumptions,
        run_context=run_context,
        mechanism_graphs=mechanism_graphs,
    )
    output_path.write_text(text, encoding="utf-8")
    from services.mechanism.browser_pdf import BrowserPdfUnavailable, html_to_pdf_bytes
    from services.mechanism.html_report import build_mechanism_html

    html = build_mechanism_html(
        top,
        assumptions=assumptions,
        run_context=run_context,
    )
    output_path.with_suffix(".html").write_text(html, encoding="utf-8")
    if write_pdf:
        pdf_path = output_path.with_suffix(".pdf")
        try:
            pdf_path.write_bytes(html_to_pdf_bytes(html))
        except BrowserPdfUnavailable:
            if mark_degraded is not None:
                mark_degraded("html_pdf_renderer_unavailable")
            from services.mechanism.pdf_export import markdown_to_pdf_bytes

            pdf_path.write_bytes(markdown_to_pdf_bytes(text))
    return output_path
