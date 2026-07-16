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


def _formula_markup(value: Any) -> str:
    """Render semicolon-delimited model expressions as compact math rows."""
    parts = [part.strip() for part in str(value or "").split(";") if part.strip()]
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
    citations = list(mol.citations or [])
    citation_rows = "".join(
        f"<li><code>{_e(c.evidence_id)}</code><span>{_e(c.source)} · "
        f"{_e(c.endpoint or '未标注终点')} · {_e(c.direction)}</span></li>"
        for c in citations[:8]
    ) or "<li class='muted'>无候选级结构化引用；当前主要由代理分支持。</li>"
    audit_missing = "、".join(mol.audit_missing) if mol.audit_missing else "无"
    risk_class = "risk" if mol.scientific_status in {"risk_evidence_only", "identity_review_required"} else "neutral"
    score_pct = max(0, min(100, mol.selection_score * 100))
    lipid_pct = max(0, min(100, mol.lipid_score * 100))
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
        </div>
      </header>

      <section class="score-grid">
        <div class="score primary"><span>竞赛代理分</span><strong>{mol.selection_score:.3f}</strong><small>效果 × 新颖性</small></div>
        <div class="score"><span>效果代理</span><strong>{mol.effect_proxy_score:.3f}</strong><small>池内第 {mol.effect_rank or '-'} 名</small></div>
        <div class="score"><span>新颖性代理</span><strong>{mol.novelty_proxy_score:.3f}</strong><small>池内第 {mol.novelty_rank or '-'} 名</small></div>
        <div class="score"><span>降脂代理</span><strong>{mol.lipid_score:.3f}</strong><small>非实验降幅</small></div>
        <div class="score"><span>毒性风险</span><strong>{mol.tox_risk:.3f}</strong><small>越低越优</small></div>
        <div class="score"><span>旧诊断分</span><strong>{mol.final_score:.3f}</strong><small>不作为官方分</small></div>
      </section>

      <section class="signal-grid">
        <div class="signal"><div class="signal-head"><span>综合优先级</span><b>{mol.selection_score:.3f}</b></div><div class="bar"><i style="width:{score_pct:.1f}%"></i></div><small>效果 × 新颖性 · 池内相对排序</small></div>
        <div class="signal"><div class="signal-head"><span>降脂潜力代理</span><b>{mol.lipid_score:.3f}</b></div><div class="bar purple"><i style="width:{lipid_pct:.1f}%"></i></div><small>尚无同条件实验读出</small></div>
      </section>

      <section class="two-col">
        <div class="panel">
          <h3>结构与排序依据</h3>
          <dl>
            <dt>SMILES</dt><dd class="wrap-code"><code>{_e(mol.smiles)}</code></dd>
            <dt>骨架</dt><dd class="wrap-code"><code>{_e(mol.scaffold_smiles or '未解析')}</code></dd>
            <dt>最近参照</dt><dd>{_e(mol.novelty_nearest_reference or '无')}（相似度 {mol.novelty_max_similarity:.3f}）</dd>
          </dl>
          <div class="formula-label">结构与性质表达式</div>
          {_formula_markup(mol.lipid_rationale)}
        </div>
        <div class="panel">
          <h3>毒性与证据边界</h3>
          <dl>
            <dt>毒性判断</dt><dd>{_formula_markup(mol.tox_rationale)}</dd>
            <dt>脂质证据</dt><dd>{_e(mol.lipid_evidence_status)}</dd>
            <dt>毒性证据</dt><dd>{_e(mol.toxicity_evidence_status)}</dd>
            <dt>审计缺口</dt><dd>{_e(audit_missing)}</dd>
          </dl>
        </div>
      </section>

      <section class="mechanism-box">
        <div><span class="eyebrow">可检验机制假说</span><h3>{_e(pathway_id)} · {_e(pathway_name)}</h3></div>
        <p><b>候选靶点：</b>{_e(targets)}　<b>依据：</b>{_e(support)}</p>
        <p><b>最小验证：</b>先在固定 10 μM 下完成脂质读出与 CCK-8 平行双终点；若命中，再通过通路标志物、拮抗剂或敲低救援验证方向。</p>
      </section>

      <details class="evidence" open>
        <summary>候选级引用与查询记录</summary>
        <ul>{citation_rows}</ul>
      </details>
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
  .content {{ padding:12mm 14mm 18mm; }}
  .candidate-card {{ border:1px solid #e9edf2; border-radius:20px; padding:16px 18px; margin:0 0 9mm; background:#fff; box-shadow:0 2px 8px rgba(15,23,42,.035); break-inside:avoid-page; page-break-inside:avoid; }}
  .candidate-head {{ display:flex; justify-content:space-between; gap:12px; border-bottom:1px solid var(--line); padding-bottom:11px; }}
  .candidate-head h2 {{ margin:2px 0; font-size:24px; color:var(--primary-dark); font-weight:900; }}
  .eyebrow {{ color:var(--primary); font-size:10px; font-weight:800; letter-spacing:.12em; }}
  .identity {{ color:var(--muted); font-size:10px; }}
  .badges {{ display:flex; gap:6px; align-items:flex-start; flex-wrap:wrap; justify-content:flex-end; }}
  .badge {{ padding:5px 10px; border-radius:999px; border:0; font-size:9px; font-weight:900; letter-spacing:.025em; white-space:nowrap; }}
  .eligible {{ color:#005338; background:#dff7eb; }} .neutral {{ color:#255f7c; background:#e8f3f8; }} .risk {{ color:#92400e; background:#fff1cc; }}
  .score-grid {{ display:grid; grid-template-columns:repeat(6,1fr); gap:7px; margin:12px 0; }}
  .score {{ min-width:0; background:#f6f8f7; border-radius:9px; padding:9px; }}
  .score.primary {{ background:linear-gradient(135deg,var(--primary),var(--purple)); color:white; }}
  .score span,.score small {{ display:block; font-size:9px; color:#667085; }} .score strong {{ display:block; font-size:20px; line-height:1.25; font-weight:900; }}
  .score.primary span,.score.primary small {{ color:#e0e7ff; }} .score.primary strong {{ color:#fff; }}
  .signal-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:9px; margin:5px 0 10px; }}
  .signal {{ background:var(--slate); border-radius:14px; padding:10px 12px; }}
  .signal-head {{ display:flex; justify-content:space-between; font-size:10px; font-weight:800; color:#191c1e; }}
  .signal-head b {{ color:var(--primary); font-variant-numeric:tabular-nums; }}
  .bar {{ height:7px; margin:7px 0 5px; border-radius:999px; background:#dfe3ea; overflow:hidden; }}
  .bar i {{ display:block; height:100%; border-radius:999px; background:linear-gradient(90deg,var(--green),#34d399); }}
  .bar.purple i {{ background:linear-gradient(90deg,var(--primary),var(--purple)); }}
  .signal small {{ color:var(--muted); font-size:9px; }}
  .two-col {{ display:grid; grid-template-columns:1fr 1fr; gap:9px; }}
  .panel {{ background:var(--slate); border:0; border-radius:14px; padding:11px 13px; }}
  .panel h3,.mechanism-box h3 {{ margin:0 0 6px; font-size:14px; font-weight:800; }}
  dl {{ margin:0; }} dt {{ color:#98a2b3; font-size:9px; font-weight:800; text-transform:uppercase; }} dd {{ margin:0 0 6px; color:#667085; }}
  .wrap-code {{ overflow-wrap:anywhere; word-break:break-all; }} code {{ font-family:"SFMono-Regular",Consolas,monospace; font-size:9px; }}
  .formula-label {{ margin:8px 0 4px; color:#98a2b3; font-size:9px; font-weight:800; letter-spacing:.04em; text-transform:uppercase; }}
  .formula-block {{ display:grid; gap:0; padding:5px 9px; border:0; border-left:3px solid var(--primary); border-radius:0 9px 9px 0; background:#f8f9fc; }}
  .formula-row {{ padding:4px 0; color:#344054; font-family:"STIX Two Math","Cambria Math","Times New Roman","Noto Serif CJK SC",serif; font-size:11px; font-weight:600; line-height:1.35; letter-spacing:.01em; font-variant-numeric:tabular-nums lining-nums; overflow-wrap:anywhere; }}
  .formula-row + .formula-row {{ border-top:1px solid #e8ebf2; }}
  .formula-row sub {{ color:#667085; font-size:.72em; font-style:normal; vertical-align:-.25em; }}
  .formula-op {{ margin:0 .18em; color:var(--primary); font-family:"STIX Two Math","Cambria Math","Times New Roman",serif; font-weight:700; }}
  .formula-empty {{ color:#98a2b3; font-size:10px; font-style:italic; }}
  .mechanism-box {{ margin-top:9px; padding:11px 13px; border:1px solid #d9d7ff; border-radius:14px; background:linear-gradient(90deg,#f2f1ff,#fbfaff); }}
  .mechanism-box p {{ margin:4px 0; color:#667085; }} .mechanism-box p b {{ color:#191c1e; font-weight:800; }}
  details.evidence {{ margin-top:8px; border-top:1px dashed var(--line); padding-top:7px; }} summary {{ font-weight:700; color:#435047; }}
  .evidence ul {{ display:grid; grid-template-columns:1fr 1fr; gap:3px 14px; padding-left:18px; margin:7px 0 0; }} .evidence li span {{ margin-left:5px; color:var(--muted); }} .muted {{ color:var(--muted); }}
  .footer {{ color:var(--muted); font-size:9px; text-align:center; padding:0 0 8mm; }}
  @page {{ size:A4; margin:10mm; }}
  @media print {{ html,body {{ background:#fff; }} .report {{ width:auto; margin:0; background:#fff; box-shadow:none; }} .cover {{ min-height:277mm; background:#fff; }} .content {{ padding:4mm 2mm; background:#fff; }} .dash-card,.candidate-card {{ box-shadow:none; }} .candidate-card {{ margin-bottom:6mm; }} a {{ color:inherit; text-decoration:none; }} }}
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
