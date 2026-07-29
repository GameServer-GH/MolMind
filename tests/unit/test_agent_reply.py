"""友好完成回复：提名表 + 机制 PDF 简述。"""

from __future__ import annotations

from packages.models import ScoreRecord
from agent.runtime.reply import (
    format_ranking_explanation,
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


def test_format_ranking_explanation_uses_frozen_result() -> None:
    top1 = _mol(
        molecule_id="T19959",
        selection_score=0.504,
        competition_scoring_version="organizer-relative-effect-novelty-v1",
        effect_rank=2,
        novelty_rank=3,
    )
    top2 = _mol(
        molecule_id="T27832",
        selection_score=0.426,
        competition_scoring_version="organizer-relative-effect-novelty-v1",
    )

    class _R:
        run_id = "mm-existing"
        top_molecules = [top1, top2]
        reserve_molecules = []
        scored_molecules = []

    text = format_ranking_explanation(_R(), molecule_id="t19959")
    assert text is not None
    assert "不会重新筛选" in text
    assert "T19959" in text
    assert "0.504" in text
    assert "T27832" in text
    assert "0.426" in text
    assert "效应相对名次 2" in text


def test_format_ranking_explanation_for_top_n_uses_current_frozen_count() -> None:
    top1 = _mol(molecule_id="T19959", selection_score=0.504)
    top2 = _mol(molecule_id="T27832", selection_score=0.426)

    class _R:
        run_id = "mm-top15"
        top_molecules = [top1, top2]
        reserve_molecules = []
        scored_molecules = []

    text = format_ranking_explanation(_R(), rank_limit=15)
    assert text is not None
    assert "实际冻结了 Top 2" in text
    assert "Top 2 的排名概览" in text
    assert "T19959" in text and "T27832" in text
    assert "不会重新筛选、导出或修改排名" in text


def test_format_ranking_explanation_keeps_two_named_ranks_distinct() -> None:
    top = [
        _mol(molecule_id=f"T{i}", selection_score=0.50 - i / 100)
        for i in range(1, 6)
    ]

    class _R:
        run_id = "mm-top4-top5"
        top_molecules = top
        reserve_molecules = []
        scored_molecules = []

    text = format_ranking_explanation(_R(), rank_positions=(4, 5))
    assert text is not None
    assert "你点名的是Top 4、Top 5" in text
    assert "| Top 4 | T4 |" in text
    assert "| Top 5 | T5 |" in text
    assert "Top 4 与 Top 5 的组合排序分相差" in text
    assert "不会重新筛选、导出或修改排名" in text


def test_format_ranking_explanation_resolves_named_rank_to_one_molecule() -> None:
    top = [
        _mol(molecule_id=f"T{i}", selection_score=0.50 - i / 100)
        for i in range(1, 6)
    ]

    class _R:
        run_id = "mm-top5-introduction"
        top_molecules = top
        reserve_molecules = []
        scored_molecules = []

    text = format_ranking_explanation(
        _R(), rank_positions=(5,), rank_position_subject=True
    )
    assert text is not None
    assert "T5 已经进入上一轮冻结主榜" in text
    assert "Top 5" in text
    assert "所以 Top 5 表示" in text
    assert "所以 Top1" not in text
    assert "Top 1 的排名概览" not in text


def test_format_ranking_explanation_resolves_rank_after_cutoff_to_reserve() -> None:
    top = [
        _mol(
            molecule_id=f"T{i}",
            selection_score=0.60 - i / 100,
            competition_scoring_version="organizer-relative-effect-novelty-v1",
        )
        for i in range(1, 11)
    ]
    reserve = [
        _mol(
            molecule_id="R11",
            selection_score=0.489,
            competition_scoring_version="organizer-relative-effect-novelty-v1",
        )
    ]

    class _R:
        run_id = "mm-top10-reserve"
        top_molecules = top
        reserve_molecules = reserve
        scored_molecules = [*top, *reserve]

    text = format_ranking_explanation(
        _R(), rank_limit=11, rank_positions=(11,), rank_position_subject=True
    )
    assert text is not None
    assert "`R11`" in text
    assert "整体 Top 11" in text
    assert "候补第 1 位" in text
    assert "主榜名额固定为 Top 10" in text
    assert "Top 10 `T10`" in text
    assert "Top 10 的排名概览" not in text
