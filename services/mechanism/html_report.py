"""Structured HTML report for browser preview and Chromium PDF export."""

from __future__ import annotations

from html import escape
import re
from typing import Any

from packages.goldset.hypothesis import ensure_fp_bits, infer_hypothesis_pathway
from packages.models import ScoreRecord


def _assumption(assumptions: dict[str, Any] | None, key: str, default: Any) -> Any:
    for row in (assumptions or {}).get("assumptions") or []:
        if isinstance(row, dict) and row.get("id") == key:
            return row.get("value", default)
    return default


def _e(value: Any) -> str:
    return escape(str(value if value is not None else ""), quote=True)


def _status_label(mol: ScoreRecord) -> str:
    labels = {
        "proxy_only": "仅计算代理",
        "risk_evidence_only": "仅风险证据",
        "mechanism_support_only": "仅机制支持",
        "candidate_activity_evidence": "候选级活性证据",
        "task_specific_experiment_supported": "同条件双终点支持",
        "identity_review_required": "身份待复核",
    }
    return labels.get(mol.scientific_status, mol.scientific_status)


def _claim_ceiling_zh(value: str) -> str:
    labels = {
        "proxy_nomination": "代理提名（不可当作已验证药效/安全性）",
        "risk_signal_only": "仅风险信号，不可外推为安全结论",
        "mechanism_hypothesis_only": "仅机制假说层",
        "task_evidence_supported": "已有任务相关实验证据支持",
    }
    return labels.get(value, value or "未标注")


def _audit_gap_zh(gap: str) -> str:
    labels = {
        "lipid_activity": "同条件降脂实验读出",
        "safety_clearance": "安全清除/低毒实验证据",
        "identity": "结构身份锁定",
        "mechanism": "候选级机制通路证据",
    }
    return labels.get(gap, gap)


def _channel_query_zh(status: str) -> str:
    labels = {
        "not_queried": "本轮未形成可计分查询结果",
        "verified_empty": "已检索且为空记录（空≠阴性生物学标签）",
        "hit": "已命中本地/外源记录",
        "query_failed": "查询失败，结果不可当作阴性",
        "network_error": "网络失败，结果不可当作阴性",
        "audit_missing": "审计缺失，尚不可用",
    }
    return labels.get(status, status or "状态未标注")


def _narrate_public_source(name: str, row: Any) -> str:
    if not isinstance(row, dict):
        return f"{name}：本轮未查询。"
    status = str(row.get("status") or "not_queried")
    hits = int(row.get("hit_count") or 0)
    scored = int(row.get("scored_hit_count") or 0)
    effect = str(row.get("ranking_effect") or "none")
    if status == "not_queried":
        return f"{name}：本轮未查询。"
    if scored > 0:
        return (
            f"{name}：检索到 {hits} 条记录，其中 {scored} 条进入计分"
            f"（效应={effect}）。"
        )
    if hits > 0 or status == "verified_empty":
        if "annotation" in effect or effect in {"none", "annotation_or_audit_only"}:
            return (
                f"{name}：已检索到注释/审计级记录（约 {max(hits, 1)} 条痕迹），"
                "但不计分、不抬排名。"
            )
        return (
            f"{name}：已检索但无计分命中（hits={hits}，scored=0）；"
            "不把空结果写成安全或无效。"
        )
    if status == "query_failed":
        return f"{name}：查询失败，本轮忽略该源，不据此下阴性结论。"
    return f"{name}：{_channel_query_zh(status)}。"


def _narrate_epa(epa: dict[str, Any]) -> str:
    if not epa:
        return "EPA CompTox/ToxCast：本轮无审计信息。"
    stage = int(epa.get("stage") or 0)
    status = str(epa.get("status") or "disabled")
    query = str(epa.get("query_status") or "not_queried")
    mapping = str(epa.get("mapping_status") or "audit_missing")
    dtxsid = str(epa.get("dtxsid") or "").strip()
    active = int(epa.get("active_hit_count") or 0)
    risk_tier = str(epa.get("cytotox_risk_tier") or "none")
    risk_applied = bool(epa.get("risk_applied", False))
    nhit = epa.get("nhit")
    lower = epa.get("cytotox_lower_um")
    basis = str(epa.get("mapping_basis") or "")

    if stage <= 0 or status in {"disabled", ""}:
        return "EPA CompTox/ToxCast：通道未启用或未参与本轮计分。"

    if not dtxsid or mapping in {"audit_missing", ""} or query == "not_queried":
        return (
            "EPA CompTox/ToxCast：未建立可用身份映射（无 DTXSID 或映射审计缺失），"
            "因此本候选未纳入 CTX 风险计分。"
        )

    parts = [f"已映射至 {dtxsid}"]
    if basis:
        parts.append(f"映射依据为 {basis}")
    if query == "identity_review_required" or "structure_audit" in mapping:
        parts.append("身份仍建议人工核对，但不因此取消计算资格")
    if query == "verified_empty" or status == "verified_empty":
        parts.append("CTX 返回空记录（空≠安全）")
    elif status == "bioactivity_annotation" or risk_tier == "bioactivity_annotation":
        parts.append(
            f"仅有体外活性注释（active assays≈{active}），未达强细胞毒计分条件"
        )
    elif risk_tier == "weak_risk_review":
        detail = []
        if nhit not in (None, ""):
            detail.append(f"nhit={nhit}")
        if lower not in (None, ""):
            detail.append(f"cytotox下限≈{lower} μM")
        extra = f"（{', '.join(detail)}）" if detail else ""
        parts.append(f"存在弱细胞毒审计信号{extra}，未自动降级")
    elif risk_tier == "strong_risk":
        detail = []
        if nhit not in (None, ""):
            detail.append(f"nhit={nhit}")
        if lower not in (None, ""):
            detail.append(f"cytotox下限≈{lower} μM")
        extra = f"（{', '.join(detail)}）" if detail else ""
        if risk_applied:
            parts.append(f"强细胞毒风险{extra}已计入毒性分")
        else:
            parts.append(f"检出强细胞毒信号{extra}，但本轮未计入毒性分")
    elif active > 0:
        parts.append(f"记录到约 {active} 个 active assay，按当前规则未作为强风险计分")
    else:
        parts.append("无额外强风险计分信号")

    return "EPA CompTox/ToxCast：" + "；".join(parts) + "。"


def _narrate_dili(dili: dict[str, Any]) -> str:
    if not dili:
        return "DILIrank：本轮无精确身份比对结果。"
    status = str(dili.get("status") or "disabled")
    action = str(dili.get("action") or "none")
    name = str(dili.get("compound_name") or "").strip()
    concern = str(dili.get("concern") or "").strip()
    if status in {"disabled", "not_queried", ""}:
        return "DILIrank：本轮未启用或未查询。"
    if status == "no_exact_match":
        return "DILIrank：无精确身份命中，未触发肝损伤硬门控。"
    who = f"命中 {name}" if name else "命中已知记录"
    concern_txt = f"，关注度 {concern}" if concern else ""
    if action and action != "none":
        return f"DILIrank：{who}{concern_txt}；处置={action}。"
    return f"DILIrank：{who}{concern_txt}；未额外改榜。"


def _narrate_claim_and_gaps(mol: ScoreRecord) -> str:
    gaps = [str(x) for x in (mol.audit_missing or ()) if str(x).strip()]
    ceiling = _claim_ceiling_zh(str(mol.claim_ceiling or ""))
    if not gaps:
        return f"声明上限：{ceiling}；当前无额外审计缺口标记。"
    gap_txt = "、".join(_audit_gap_zh(g) for g in gaps)
    return f"声明上限：{ceiling}。仍缺：{gap_txt}。"


def _narrate_tox_summary(mol: ScoreRecord) -> str:
    alert = float((mol.tox_heads or {}).get("alert") or 0.0)
    evidence = float((mol.tox_heads or {}).get("evidence") or 0.0)
    bits: list[str] = [f"综合毒性风险代理分为 {mol.tox_risk:.3f}（越低越优）"]
    if alert >= 0.2:
        bits.append(f"结构警示分量 {alert:.2f}，实验需重点看活力/干扰")
    if evidence >= 0.25:
        bits.append(f"外部毒性证据分量 {evidence:.2f}，仅作风险提示")
    if mol.safety_clearance_confidence <= 1e-12:
        bits.append("尚无安全清除证据，不能写成低毒结论")
    return "；".join(bits) + "。"


def build_evidence_boundary_narrative(
    mol: ScoreRecord,
    *,
    compact: bool = False,
) -> list[str]:
    """Human-readable evidence-boundary sentences for mechanism HTML/PDF."""
    src = mol.evidence_source_audit or {}
    if compact:
        public = "；".join(
            [
                _narrate_public_source("ChEMBL", src.get("chembl")).removeprefix("ChEMBL："),
                _narrate_public_source("PubChem", src.get("pubchem")).removeprefix("PubChem："),
                _narrate_public_source("BindingDB", src.get("bindingdb")).removeprefix(
                    "BindingDB："
                ),
            ]
        )
        lines = [
            _narrate_claim_and_gaps(mol),
            _narrate_tox_summary(mol),
            _narrate_epa(dict(mol.epa_audit or {})),
            f"公共库检索：{public}",
        ]
        dili = _narrate_dili(dict(mol.dili_audit or {}))
        if "无精确身份命中" not in dili and "未启用" not in dili and "无精确身份比对" not in dili:
            lines.append(dili)
        return lines

    return [
        (
            f"脂质证据通道：{_channel_query_zh(str(mol.lipid_evidence_status or 'not_queried'))}；"
            "排名主要依赖结构/性质代理，而非同条件实验降脂读出。"
        ),
        (
            f"毒性证据通道：{_channel_query_zh(str(mol.toxicity_evidence_status or 'not_queried'))}。"
        ),
        _narrate_tox_summary(mol),
        _narrate_epa(dict(mol.epa_audit or {})),
        _narrate_public_source("ChEMBL", src.get("chembl")),
        _narrate_public_source("PubChem", src.get("pubchem")),
        _narrate_public_source("BindingDB", src.get("bindingdb")),
        _narrate_dili(dict(mol.dili_audit or {})),
        _narrate_claim_and_gaps(mol),
    ]


def _evidence_boundary_html(mol: ScoreRecord) -> str:
    bullets = "".join(
        f"<li>{_e(line)}</li>"
        for line in build_evidence_boundary_narrative(mol, compact=True)
    )
    return f"""
          <h3>毒性与证据边界</h3>
          <ul class="evidence-bullets">{bullets}</ul>
          <div class="formula-label">毒性计算摘要</div>
          {_formula_markup(mol.tox_rationale, max_parts=7)}
    """


def _epa_key_facts_html(mol: ScoreRecord) -> str:
    epa = mol.epa_audit or {}
    dtxsid = str(epa.get("dtxsid") or "").strip()
    if not dtxsid:
        return ""
    tier = str(epa.get("cytotox_risk_tier") or "none")
    nhit = epa.get("nhit")
    lower = epa.get("cytotox_lower_um")
    active = int(epa.get("active_hit_count") or 0)
    applied = "已计入毒性分" if epa.get("risk_applied") else "未计入毒性分"
    inherited = str(epa.get("risk_inherited_from_dtxsid") or "").strip()
    rows = [
        f"<dt>DTXSID</dt><dd><code>{_e(dtxsid)}</code></dd>",
        f"<dt>风险层级</dt><dd>{_e(tier)} · {applied}</dd>",
        f"<dt>active assays</dt><dd>{active}</dd>",
    ]
    if nhit not in (None, ""):
        rows.append(f"<dt>nhit</dt><dd>{_e(nhit)}</dd>")
    if lower not in (None, ""):
        rows.append(f"<dt>cytotox 下限</dt><dd>{_e(lower)} μM</dd>")
    if inherited:
        rows.append(f"<dt>盐型/母体继承</dt><dd>来自 {_e(inherited)}</dd>")
    return (
        '<div class="epa-facts">'
        '<div class="formula-label">EPA 关键指标</div>'
        f"<dl>{''.join(rows)}</dl>"
        "</div>"
    )


def _why_selected_html(mol: ScoreRecord) -> str:
    reason = str(mol.selection_reason or mol.overall_reason or "").strip()
    if not reason:
        return ""
    # Keep the most informative leading clauses for PDF readability.
    return f"""
      <section class="why-box">
        <div class="formula-label">入选与排序理由</div>
        {_formula_markup(reason, max_parts=5)}
      </section>
    """


def _experiment_plan_html(mol: ScoreRecord, pathway_id: str) -> str:
    items = [
        f"固定初筛浓度 {mol.screening_concentration_um:g} μM",
        f"活力终点 {mol.viability_endpoint or 'CCK-8'}（参考 {mol.viability_threshold_reference or '>0.80'}）",
        "脂质读出与活力必须平行判读，不以单终点下结论",
    ]
    heads = mol.tox_heads or {}
    if float(heads.get("alert") or 0.0) >= 0.2:
        items.append("结构警示偏高：加做实时细胞计数 / LDH 或 ATP，并排查荧光干扰")
    if float(heads.get("evidence") or 0.0) >= 0.25:
        items.append("存在外部毒性信号：实验记录中单独标注风险读出")
    epa = mol.epa_audit or {}
    tier = str(epa.get("cytotox_risk_tier") or "")
    if epa.get("risk_applied") or tier == "strong_risk":
        items.append("EPA 强细胞毒相关：优先确认 10 μM 下活力是否可接受")
    elif tier == "weak_risk_review":
        items.append("EPA 弱风险仅审计：记录即可，不作为唯一否决依据")
    if pathway_id == "UNRESOLVED":
        items.append("通路未锁定：双终点命中后再做无偏机制解析（转录/磷酸化/救援）")
    else:
        items.append("按假说通路补做下游标志物、拮抗剂或敲低救援")
    gaps = [str(x) for x in (mol.audit_missing or ()) if str(x).strip()]
    if "lipid_activity" in gaps:
        items.append("当前缺同条件降脂实验证据，湿实验结果将直接决定主张能否升级")
    if "safety_clearance" in gaps:
        items.append("当前缺安全清除证据，不可把低 tox_risk 写成已验证低毒")
    lis = "".join(f"<li>{_e(x)}</li>" for x in items)
    return f"""
      <section class="plan-box">
        <div class="formula-label">建议实验读出清单</div>
        <ul class="plan-list">{lis}</ul>
      </section>
    """


def _formula_markup(value: Any, *, max_parts: int | None = None) -> str:
    """Render semicolon-delimited model expressions as compact math rows."""
    parts = [part.strip() for part in str(value or "").split(";") if part.strip()]
    if max_parts is not None:
        parts = parts[: max(1, max_parts)]
    if not parts:
        return '<div class="formula-empty">未提供计算表达式</div>'
    rows: list[str] = []
    for part in parts:
        rendered = _e(part)
        rendered = re.sub(
            r"\b([A-Za-z][A-Za-z0-9]*)_([A-Za-z][A-Za-z0-9]*)\b",
            r"\1<sub>\2</sub>",
            rendered,
        )
        rendered = rendered.replace("=", '<span class="formula-op">=</span>')
        rows.append(f'<div class="formula-row">{rendered}</div>')
    return '<div class="formula-block">' + "".join(rows) + "</div>"


def _candidate_html(rank: int, mol: ScoreRecord) -> str:
    mol.fp_bits = ensure_fp_bits(mol.smiles, mol.fp_bits)
    pathway, support = infer_hypothesis_pathway(mol.smiles, mol.fp_bits)
    pathway_id = str(pathway.get("id") or "UNRESOLVED")
    pathway_name = str(pathway.get("name") or "候选机制未解析")
    targets = ", ".join(str(x) for x in pathway.get("targets") or []) or "待定"
    if pathway_id == "UNRESOLVED":
        mechanism_support = (
            f"{support}（最近参照 {mol.novelty_nearest_reference or '无'}，"
            f"sim={mol.novelty_max_similarity:.2f}），不编造靶点。"
        )
        mechanism_validation = "先做 10 μM 脂质 + CCK-8 双终点；命中后再做无偏机制解析。"
    else:
        mechanism_support = support
        mechanism_validation = "先做 10 μM 脂质 + CCK-8 双终点；命中后再做通路标志物/救援验证。"
    risk_class = (
        "risk"
        if mol.scientific_status in {"risk_evidence_only", "identity_review_required"}
        else "neutral"
    )
    score_pct = max(0, min(100, mol.selection_score * 100))
    lipid_pct = max(0, min(100, mol.lipid_score * 100))
    # Lower tox_risk is better; invert for a "safety-ish" bar visualization.
    tox_bar_pct = max(0, min(100, (1.0 - float(mol.tox_risk)) * 100))
    return f"""
    <article class="candidate-card">
      <header class="candidate-head">
        <div>
          <div class="eyebrow">PRIMARY #{rank:02d}</div>
          <h2>{_e(mol.molecule_id)}</h2>
          <div class="identity">InChIKey: <code>{_e(mol.inchikey or 'n/a')}</code> · CAS: {_e(mol.cas or 'n/a')}</div>
        </div>
        <div class="badges">
          <span class="badge eligible">计算资格通过</span>
          <span class="badge {risk_class}">{_e(_status_label(mol))}</span>
          <span class="badge claim">{_e(_claim_ceiling_zh(str(mol.claim_ceiling or '')))}</span>
        </div>
      </header>

      <section class="score-grid">
        <div class="score primary"><span>筛选代理分</span><strong>{mol.selection_score:.3f}</strong><small>效果 × 新颖性</small></div>
        <div class="score"><span>效果代理</span><strong>{mol.effect_proxy_score:.3f}</strong><small>池内 #{mol.effect_rank or '-'}</small></div>
        <div class="score"><span>新颖性代理</span><strong>{mol.novelty_proxy_score:.3f}</strong><small>池内 #{mol.novelty_rank or '-'}</small></div>
        <div class="score"><span>降脂代理</span><strong>{mol.lipid_score:.3f}</strong><small>非实验降幅</small></div>
        <div class="score"><span>毒性风险</span><strong>{mol.tox_risk:.3f}</strong><small>越低越优</small></div>
      </section>

      <section class="signal-grid three">
        <div class="signal">
          <div class="signal-head"><span>综合优先级</span><b>{mol.selection_score:.3f}</b></div>
          <div class="bar"><i style="width:{score_pct:.1f}%"></i></div>
          <small>效果 × 新颖性 · 池内相对排序</small>
        </div>
        <div class="signal">
          <div class="signal-head"><span>降脂潜力代理</span><b>{mol.lipid_score:.3f}</b></div>
          <div class="bar purple"><i style="width:{lipid_pct:.1f}%"></i></div>
          <small>尚无同条件实验读出</small>
        </div>
        <div class="signal">
          <div class="signal-head"><span>低毒余量代理</span><b>{(1.0 - float(mol.tox_risk)):.3f}</b></div>
          <div class="bar teal"><i style="width:{tox_bar_pct:.1f}%"></i></div>
          <small>由 1−tox_risk 可视化；非安全证明</small>
        </div>
      </section>

      <section class="two-col">
        <div class="panel">
          <h3>结构与排序依据</h3>
          <dl>
            <dt>SMILES</dt><dd class="wrap-code"><code>{_e(mol.smiles)}</code></dd>
            <dt>最近参照</dt><dd>{_e(mol.novelty_nearest_reference or '无')}（相似度 {mol.novelty_max_similarity:.3f}）</dd>
            <dt>骨架</dt><dd class="wrap-code"><code>{_e(mol.scaffold_smiles or '未解析')}</code></dd>
          </dl>
          <div class="formula-label">结构与性质表达式</div>
          {_formula_markup(mol.lipid_rationale, max_parts=6)}
          {_epa_key_facts_html(mol)}
        </div>
        <div class="panel">
          {_evidence_boundary_html(mol)}
        </div>
      </section>

      {_why_selected_html(mol)}

      <section class="mechanism-box">
        <div><span class="eyebrow">可检验机制假说</span><h3>{_e(pathway_id)} · {_e(pathway_name)}</h3></div>
        <p><b>靶点：</b>{_e(targets)}　<b>依据：</b>{_e(mechanism_support)}</p>
        <p><b>最小验证：</b>{_e(mechanism_validation)}</p>
      </section>

      {_experiment_plan_html(mol, pathway_id)}
    </article>
    """


def build_mechanism_html(
    top: list[ScoreRecord],
    *,
    assumptions: dict[str, Any] | None = None,
    run_context: dict[str, str] | None = None,
    title: str = "MolMind 机制与验证方案",
) -> str:
    if any(m.eligibility_status != "eligible" or m.gated_out for m in top):
        raise ValueError("HTML 机制报告只能包含最终 eligible 候选")
    concentration = float(_assumption(assumptions, "screening_concentration", 10))
    viability_pct = 100.0 * float(_assumption(assumptions, "viability_proxy", 0.80))
    lineage = run_context or {}
    candidates = "".join(_candidate_html(rank, mol) for rank, mol in enumerate(top, 1))
    lead = top[0] if top else None
    lead_id = lead.molecule_id if lead else "暂无候选"
    lead_score = float(lead.selection_score) if lead else 0.0
    lead_effect = float(lead.effect_proxy_score) if lead else 0.0
    lead_novelty = float(lead.novelty_proxy_score) if lead else 0.0
    lead_lipid = float(lead.lipid_score) if lead else 0.0
    lead_tox = float(lead.tox_risk) if lead else 1.0
    lead_status = _status_label(lead) if lead else "无数据"
    gauge_deg = max(0.0, min(360.0, lead_score * 360.0))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<style>
  :root {{ --ink:#191c1e; --muted:#5f6472; --line:#e1e4e8; --paper:#fff; --slate:#f7f9fb; --slate-2:#eef1f4; --primary:#4f46e5; --primary-dark:#3525cd; --primary-soft:#eef2ff; --purple:#6366f1; --green:#059669; --green-soft:#e8f8f1; --amber:#a16207; --amber-soft:#fff7df; --blue:#256b8a; --blue-soft:#eaf4f8; }}
  * {{ box-sizing:border-box; }}
  html {{ background:#fff; }}
  body {{ margin:0; color:#667085; background:#fff; font-family:"Inter","Noto Sans CJK SC","PingFang SC","Microsoft YaHei","Arial Unicode MS",Arial,sans-serif; font-size:13px; line-height:1.55; font-weight:400; }}
  h1,h2,h3,strong,b,dt,summary,.candidate-head h2,.score strong,.cover-stat b {{ color:#191c1e; font-weight:800; }}
  p,dd,small,.identity,.lineage,.footer {{ color:#667085; }}
  .report {{ width:210mm; margin:18px auto; background:#fff; box-shadow:none; }}
  .cover {{ min-height:270mm; padding:17mm 18mm 18mm; background:#fff; position:relative; page-break-after:always; }}
  .cover-top {{ display:flex; justify-content:space-between; align-items:center; padding-bottom:7mm; border-bottom:1px solid #e5e7eb; }}
  .brand {{ color:var(--primary); font-size:11px; font-weight:900; letter-spacing:.14em; text-transform:uppercase; }}
  .cover-status {{ color:#93000a; background:#ffdad6; border:0; border-radius:999px; padding:5px 11px; font-size:9px; font-weight:900; letter-spacing:.06em; text-transform:uppercase; }}
  h1 {{ margin:18mm 0 5mm; max-width:165mm; font-size:36px; line-height:1.12; letter-spacing:-.045em; color:var(--primary-dark); font-weight:900; }}
  .subtitle {{ max-width:155mm; margin:0; font-size:16px; line-height:1.7; color:#667085; }}
  .cover-rule {{ width:42mm; height:4px; margin-top:8mm; border-radius:999px; background:var(--primary); }}
  .cover-params {{ display:flex; gap:7px; margin-top:9mm; flex-wrap:wrap; }}
  .cover-param {{ padding:5px 10px; border-radius:999px; background:#f8fafc; border:1px solid #e5e7eb; color:#667085; font-size:9px; }}
  .cover-param b {{ color:#344054; font-weight:900; }}
  .dashboard-label {{ margin-top:11mm; color:#98a2b3; font-size:9px; font-weight:900; letter-spacing:.12em; text-transform:uppercase; }}
  .cover-dashboard {{ display:grid; grid-template-columns:5fr 7fr; gap:10px; margin-top:7px; }}
  .dash-card {{ border:1px solid #e5e7eb; border-radius:16px; background:#fff; padding:14px; box-shadow:none; }}
  .dash-card h3 {{ margin:0 0 8px; color:#191c1e; font-size:15px; font-weight:900; }}
  .lead-card {{ display:flex; flex-direction:column; min-height:91mm; }}
  .lead-top {{ display:flex; justify-content:space-between; gap:8px; align-items:flex-start; }}
  .lead-id {{ color:var(--primary-dark); font-size:19px; font-weight:900; overflow-wrap:anywhere; }}
  .evidence-pill {{ flex:0 0 auto; padding:5px 9px; border-radius:999px; color:var(--blue); background:var(--blue-soft); font-size:8px; font-weight:900; letter-spacing:.03em; }}
  .gauge {{ width:42mm; height:42mm; margin:9px auto; border-radius:50%; display:grid; place-items:center; background:conic-gradient(var(--primary) 0deg,var(--purple) {gauge_deg:.1f}deg,#e9edf3 {gauge_deg:.1f}deg 360deg); position:relative; }}
  .gauge::after {{ content:""; position:absolute; inset:7px; border-radius:50%; background:#fff; }}
  .gauge-value {{ position:relative; z-index:1; text-align:center; }}
  .gauge-value b {{ display:block; color:var(--primary-dark); font-size:25px; font-weight:900; font-variant-numeric:tabular-nums; }}
  .gauge-value small {{ color:#98a2b3; font-size:8px; font-weight:800; }}
  .lead-tags {{ display:grid; gap:5px; margin-top:auto; }}
  .lead-tag {{ padding:7px 9px; border-radius:9px; background:#f7f7ff; color:#5a5f72; font-size:9px; }}
  .lead-tag b {{ color:var(--primary-dark); font-weight:900; }}
  .dash-stack {{ display:grid; gap:10px; }}
  .efficacy-card {{ min-height:50mm; }}
  .metric {{ margin-top:8px; }}
  .metric-head {{ display:flex; justify-content:space-between; gap:8px; color:#667085; font-size:9px; font-weight:700; }}
  .metric-head b {{ color:#344054; font-weight:900; font-variant-numeric:tabular-nums; }}
  .metric-track {{ height:7px; margin-top:4px; border-radius:999px; background:#e9edf3; overflow:hidden; }}
  .metric-track i {{ display:block; height:100%; border-radius:999px; background:linear-gradient(90deg,var(--primary),var(--purple)); }}
  .metric-track.green i {{ background:linear-gradient(90deg,var(--green),#34d399); }}
  .constraint-card {{ min-height:31mm; background:#fafaff; border-color:#dedcff; }}
  .constraint-grid {{ display:grid; grid-template-columns:auto 1fr; gap:10px; align-items:start; }}
  .risk-value {{ min-width:18mm; padding:8px; border-radius:10px; background:#fff; color:var(--primary-dark); text-align:center; font-size:17px; font-weight:900; font-variant-numeric:tabular-nums; }}
  .constraint-card p {{ margin:0; color:#667085; font-size:9px; line-height:1.6; }}
  .constraint-note {{ margin-top:7px; color:#98a2b3; font-size:8px; font-weight:700; }}
  .lineage {{ display:none; }}
  .content {{ padding:8mm 10mm 12mm; }}
  .candidate-card {{ border:1px solid #e9edf2; border-radius:14px; padding:12px 14px; margin:0 0 0; background:#fff; box-shadow:none; break-inside:avoid-page; page-break-inside:avoid; page-break-after:always; }}
  .candidate-card:last-of-type {{ page-break-after:auto; }}
  .candidate-head {{ display:flex; justify-content:space-between; gap:10px; border-bottom:1px solid var(--line); padding-bottom:7px; }}
  .candidate-head h2 {{ margin:1px 0; font-size:20px; color:var(--primary-dark); font-weight:900; }}
  .eyebrow {{ color:var(--primary); font-size:9px; font-weight:800; letter-spacing:.12em; }}
  .identity {{ color:var(--muted); font-size:9px; }}
  .badges {{ display:flex; gap:5px; align-items:flex-start; flex-wrap:wrap; justify-content:flex-end; }}
  .badge {{ padding:4px 8px; border-radius:999px; border:0; font-size:8px; font-weight:900; letter-spacing:.025em; white-space:nowrap; }}
  .eligible {{ color:#005338; background:#dff7eb; }} .neutral {{ color:#255f7c; background:#e8f3f8; }} .risk {{ color:#92400e; background:#fff1cc; }}
  .claim {{ color:#5b21b6; background:#f3e8ff; max-width:42mm; white-space:normal; text-align:right; line-height:1.25; }}
  .score-grid {{ display:grid; grid-template-columns:repeat(5,1fr); gap:5px; margin:8px 0 6px; }}
  .score {{ min-width:0; background:#f6f8f7; border-radius:8px; padding:7px 8px; }}
  .score.primary {{ background:linear-gradient(135deg,var(--primary),var(--purple)); color:white; }}
  .score span,.score small {{ display:block; font-size:8px; color:#667085; }} .score strong {{ display:block; font-size:16px; line-height:1.2; font-weight:900; }}
  .score.primary span,.score.primary small {{ color:#e0e7ff; }} .score.primary strong {{ color:#fff; }}
  .signal-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:7px; margin:0 0 8px; }}
  .signal-grid.three {{ grid-template-columns:1fr 1fr 1fr; }}
  .signal {{ background:var(--slate); border-radius:10px; padding:8px 10px; }}
  .signal-head {{ display:flex; justify-content:space-between; font-size:9px; font-weight:800; color:#191c1e; }}
  .signal-head b {{ color:var(--primary); font-variant-numeric:tabular-nums; }}
  .bar {{ height:6px; margin:6px 0 4px; border-radius:999px; background:#dfe3ea; overflow:hidden; }}
  .bar i {{ display:block; height:100%; border-radius:999px; background:linear-gradient(90deg,var(--green),#34d399); }}
  .bar.purple i {{ background:linear-gradient(90deg,var(--primary),var(--purple)); }}
  .bar.teal i {{ background:linear-gradient(90deg,#0f766e,#2dd4bf); }}
  .signal small {{ color:var(--muted); font-size:8px; }}
  .two-col {{ display:grid; grid-template-columns:1fr 1.15fr; gap:8px; }}
  .panel {{ background:var(--slate); border:0; border-radius:10px; padding:9px 11px; }}
  .panel h3,.mechanism-box h3 {{ margin:0 0 4px; font-size:12px; font-weight:800; }}
  .evidence-bullets {{ margin:0 0 6px; padding-left:14px; display:grid; gap:3px; }}
  .evidence-bullets li {{ color:#435047; font-size:9px; line-height:1.4; }}
  .epa-facts {{ margin-top:6px; }}
  .epa-facts dl {{ display:grid; grid-template-columns:auto 1fr; gap:2px 8px; }}
  .epa-facts dt {{ margin:0; }} .epa-facts dd {{ margin:0; }}
  .why-box,.plan-box {{ margin-top:8px; padding:8px 10px; border-radius:10px; background:#f8fafc; border:1px solid #e8edf3; }}
  .plan-list {{ margin:0; padding-left:15px; display:grid; gap:3px; }}
  .plan-list li {{ color:#435047; font-size:9px; line-height:1.4; }}
  dl {{ margin:0; }} dt {{ color:#98a2b3; font-size:8px; font-weight:800; text-transform:uppercase; }} dd {{ margin:0 0 4px; color:#667085; font-size:9px; }}
  .wrap-code {{ overflow-wrap:anywhere; word-break:break-all; }} code {{ font-family:"SFMono-Regular",Consolas,monospace; font-size:8px; }}
  .formula-label {{ margin:6px 0 3px; color:#98a2b3; font-size:8px; font-weight:800; letter-spacing:.04em; text-transform:uppercase; }}
  .formula-block {{ display:grid; gap:0; padding:4px 8px; border:0; border-left:3px solid var(--primary); border-radius:0 8px 8px 0; background:#f8f9fc; }}
  .formula-row {{ padding:3px 0; color:#344054; font-family:"STIX Two Math","Cambria Math","Times New Roman","Noto Serif CJK SC",serif; font-size:9px; font-weight:600; line-height:1.3; letter-spacing:.01em; font-variant-numeric:tabular-nums lining-nums; overflow-wrap:anywhere; }}
  .formula-row + .formula-row {{ border-top:1px solid #e8ebf2; }}
  .formula-row sub {{ color:#667085; font-size:.72em; font-style:normal; vertical-align:-.25em; }}
  .formula-op {{ margin:0 .18em; color:var(--primary); font-family:"STIX Two Math","Cambria Math","Times New Roman",serif; font-weight:700; }}
  .formula-empty {{ color:#98a2b3; font-size:9px; font-style:italic; }}
  .mechanism-box {{ margin-top:8px; padding:9px 11px; border:1px solid #d9d7ff; border-radius:10px; background:linear-gradient(90deg,#f2f1ff,#fbfaff); }}
  .mechanism-box p {{ margin:2px 0; color:#667085; font-size:9px; line-height:1.4; }} .mechanism-box p b {{ color:#191c1e; font-weight:800; }}
  .muted {{ color:var(--muted); }}
  .footer {{ color:var(--muted); font-size:9px; text-align:center; padding:0 0 8mm; }}
  @page {{ size:A4; margin:10mm; }}
  @media print {{ html,body {{ background:#fff; }} .report {{ width:auto; margin:0; background:#fff; box-shadow:none; }} .cover {{ min-height:277mm; background:#fff; }} .content {{ padding:3mm 2mm; background:#fff; }} .dash-card,.candidate-card {{ box-shadow:none; }} .candidate-card {{ page-break-after:always; }} .candidate-card:last-of-type {{ page-break-after:auto; }} a {{ color:inherit; text-decoration:none; }} }}
</style>
</head>
<body>
<main class="report">
  <section class="cover">
    <div class="cover-top">
      <div class="brand">MolMind · Auditable Nomination</div>
      <div class="cover-status">PRE-WET LAB · 计算优先级</div>
    </div>
    <h1>{_e(title)}</h1>
    <p class="subtitle">面向 HepG2-FFA 低毒降脂初筛的计算候选优先级报告。候选尚未经湿实验验证，所有结论受证据等级与声明上限约束。</p>
    <div class="cover-rule"></div>
    <div class="cover-params">
      <span class="cover-param"><b>{len(top)}</b> 个主榜候选</span>
      <span class="cover-param"><b>{concentration:g} μM</b> 固定初筛</span>
      <span class="cover-param"><b>CCK-8 &gt;{viability_pct:.0f}%</b> 活力参考</span>
      <span class="cover-param"><b>脂质 + 活力</b> 平行判读</span>
    </div>
    <div class="dashboard-label">Portfolio preview · 候选组合总览</div>
    <div class="cover-dashboard">
      <section class="dash-card lead-card">
        <div class="lead-top"><div><h3>Lead Candidate</h3><div class="lead-id">{_e(lead_id)}</div></div><span class="evidence-pill">{_e(lead_status)}</span></div>
        <div class="gauge"><div class="gauge-value"><b>{lead_score:.3f}</b><small>SELECTION SCORE</small></div></div>
        <div class="lead-tags">
          <div class="lead-tag"><b>效果 × 新颖性</b> · 池内相对排序</div>
          <div class="lead-tag"><b>计算代理</b> · 尚未经同条件实验验证</div>
        </div>
      </section>
      <div class="dash-stack">
        <section class="dash-card efficacy-card">
          <h3>Biomarker Efficacy</h3>
          <div class="metric"><div class="metric-head"><span>效果代理 · Effect</span><b>{lead_effect:.3f}</b></div><div class="metric-track green"><i style="width:{max(0,min(100,lead_effect*100)):.1f}%"></i></div></div>
          <div class="metric"><div class="metric-head"><span>新颖性代理 · Novelty</span><b>{lead_novelty:.3f}</b></div><div class="metric-track"><i style="width:{max(0,min(100,lead_novelty*100)):.1f}%"></i></div></div>
          <div class="metric"><div class="metric-head"><span>降脂潜力代理 · Lipid</span><b>{lead_lipid:.3f}</b></div><div class="metric-track"><i style="width:{max(0,min(100,lead_lipid*100)):.1f}%"></i></div></div>
          <div class="constraint-note">进度条表示候选池内计算代理，不代表实验降幅或生物标志物实测值。</div>
        </section>
        <section class="dash-card constraint-card">
          <h3>Toxicity &amp; Claim Constraints</h3>
          <div class="constraint-grid"><div class="risk-value">{lead_tox:.3f}</div><p><b>毒性风险代理，越低越优。</b><br>当前结论仅限计算优先级；脂质主读出、活力与机制均须按固定 10 μM 方案验证。</p></div>
          <div class="constraint-note">Hoechst 为辅助；脂质数值门槛仍待 SOP 确认。</div>
        </section>
      </div>
    </div>
  </section>
  <section class="content">{candidates}</section>
  <div class="footer">MolMind · 计算候选优先级，不构成已验证药效或安全性结论</div>
</main>
</body>
</html>"""


__all__ = ["build_mechanism_html"]
