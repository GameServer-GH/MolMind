"""Natural-language evidence boundary copy in mechanism HTML/PDF."""

from __future__ import annotations

from packages.models import ScoreRecord
from services.mechanism.html_report import (
    build_evidence_boundary_narrative,
    build_mechanism_html,
)


def _mol(**kwargs) -> ScoreRecord:
    base = dict(
        molecule_id="T19959",
        smiles="CCO",
        inchikey="ABOOPXYCKNFDNJ-UHFFFAOYSA-N",
        cas="76578-12-6",
        scaffold_smiles="CCO",
        lipid_score=0.4,
        tox_risk=0.345,
        novelty_score=0.74,
        conf_e=0.0,
        final_score=0.5,
        selection_score=0.5,
        tox_heads={"alert": 0.12, "evidence": 0.3},
        lipid_parts={},
        attributions=[],
        lipid_rationale="S_lipid=0.4",
        tox_rationale="R_tox=0.345",
        overall_reason="eligible",
        eligibility_status="eligible",
        gated_out=False,
        lipid_evidence_status="not_queried",
        toxicity_evidence_status="not_queried",
        claim_ceiling="proxy_nomination",
        audit_missing=("lipid_activity", "safety_clearance"),
        safety_clearance_confidence=0.0,
        novelty_nearest_reference="Fenofibrate",
        novelty_max_similarity=0.258,
        epa_audit={
            "stage": 2,
            "status": "verified_empty",
            "query_status": "identity_review_required",
            "mapping_status": "identifier_match_requires_structure_audit",
            "mapping_basis": "cas",
            "dtxsid": "DTXSID60273935",
            "active_hit_count": 0,
            "cytotox_risk_tier": "none",
            "risk_applied": False,
        },
        dili_audit={"status": "no_exact_match", "action": "none"},
        evidence_source_audit={
            "chembl": {
                "status": "verified_empty",
                "hit_count": 1,
                "scored_hit_count": 0,
                "ranking_effect": "annotation_or_audit_only",
            },
            "pubchem": {
                "status": "verified_empty",
                "hit_count": 1,
                "scored_hit_count": 0,
                "ranking_effect": "annotation_or_audit_only",
            },
            "bindingdb": {"status": "not_queried", "hit_count": 0, "scored_hit_count": 0},
        },
    )
    base.update(kwargs)
    return ScoreRecord(**base)


def test_evidence_boundary_uses_natural_language() -> None:
    lines = build_evidence_boundary_narrative(_mol())
    text = "\n".join(lines)
    assert "stage=" not in text
    assert "status=" not in text
    assert "未形成可计分查询结果" in text
    assert "DTXSID60273935" in text
    assert "身份仍建议人工核对" in text
    assert "ChEMBL：查询完成，无符合计分契约的记录" in text
    assert "同条件降脂实验读出" in text


def test_compact_narrative_is_shorter() -> None:
    full = build_evidence_boundary_narrative(_mol(), compact=False)
    compact = build_evidence_boundary_narrative(_mol(), compact=True)
    assert len(compact) < len(full)
    assert any("公共库检索" in line for line in compact)
    assert any("声明上限" in line for line in compact)


def test_annotation_only_cannot_be_narrated_as_scored() -> None:
    mol = _mol(
        evidence_source_audit={
            "chembl": {
                "status": "annotation_only",
                "hit_count": 1,
                "scored_hit_count": 1,
                "ranking_effect": "score",
            }
        }
    )
    text = "\n".join(build_evidence_boundary_narrative(mol))
    assert "ChEMBL：已检索但无计分命中" in text
    assert "其中 1 条进入计分" not in text


def test_mechanism_html_is_compact_for_pdf() -> None:
    html = build_mechanism_html(
        [_mol(selection_reason="eligibility=eligible; score=competition=0.5")],
        run_context={
            "run_id": "run-1",
            "input_sha256": "a" * 64,
            "config_hash": "config-v1",
            "selection_sha256": "b" * 64,
        },
    )
    assert "毒性与证据边界" in html
    assert "evidence-bullets" in html
    assert "stage=2; status=" not in html
    assert "声明上限" in html
    assert "ACCase" in html
    assert "候选级引用与查询记录" not in html
    assert "毒性计算摘要" in html
    assert "结构与性质表达式" in html
    assert "signal-grid" in html
    assert "毒性分量与可信度" not in html
    assert "候选特异验证与风险控制" in html
    assert "HepG2-FFA 双终点主筛与机制升级规则" in html
    assert "Scientific Reports 原始研究" not in html
    assert "入选与排序理由" in html
    assert "EPA 关键指标" in html
    assert "page-break-after:always" in html
    assert "<details" not in html
    assert "Reproducibility lineage" in html
    assert "run-1" in html
    assert "EPA 身份映射待结构复核" in html
    assert "[供应商/二级资料]" in html
    assert "其中 1 条进入计分" not in html
