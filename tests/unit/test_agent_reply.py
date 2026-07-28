"""友好完成回复：提名表 + 机制 PDF 简述。"""

from __future__ import annotations

from packages.models import ScoreRecord
from agent.runtime.reply import (
    format_run_completion,
    mechanism_pdf_blurb,
    nomination_markdown_table,
    why_nominated,
)


def _mol(**kwargs) -> ScoreRecord:
    base = dict(
        molecule_id="T001",
        smiles="CCO",
        inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
        cas=None,
        scaffold_smiles="CCO",
        lipid_score=0.4,
        tox_risk=0.3,
        novelty_score=0.75,
        conf_e=0.5,
        final_score=0.5,
        tox_heads={},
        lipid_parts={},
        attributions=[],
        lipid_rationale="多信号降脂融合；药效团: carboxylic acid, aromatic ring；LogP=3.9",
        tox_rationale="R_tox=0.3",
        overall_reason="score",
        selection_score=0.48,
        screening_concentration_um=10.0,
    )
    base.update(kwargs)
    return ScoreRecord(**base)


def test_why_nominated_uses_pharmacophore_zh() -> None:
    text = why_nominated(_mol())
    assert "羧酸" in text
    assert "芳香环" in text
    assert "毒性风险" in text


def test_nomination_table_has_rows() -> None:
    mols = [_mol(molecule_id="A"), _mol(molecule_id="B", lipid_score=0.35)]
    md = nomination_markdown_table(mols)
    assert "| 排名 | 分子 |" in md
    assert "| 1 | A |" in md
    assert "| 2 | B |" in md


def test_nomination_table_keeps_all_rows() -> None:
    mols = [_mol(molecule_id=f"M{i}") for i in range(1, 21)]
    md = nomination_markdown_table(mols)
    assert "| 1 | M1 |" in md
    assert "| 20 | M20 |" in md
    assert md.count("\n") >= 21  # header + sep + 20 rows


def test_format_run_completion_friendly_and_has_table() -> None:
    class _R:
        top_molecules = [_mol(molecule_id="T19959")]
        source_filename = "lib.sdf"

    text = format_run_completion(want_csv=True, want_pdf=True, result=_R())
    assert "已按计划完成" not in text
    assert "请点击下方卡片下载" not in text
    assert "T19959" in text
    assert "| 排名 | 分子 |" in text
    assert "机制 PDF" in text
    assert "HepG2-FFA" in mechanism_pdf_blurb(_R().top_molecules)
