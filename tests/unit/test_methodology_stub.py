"""方法学 Markdown 桩存在且含 ADR-M16 分轨表述。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "delivery" / "方法学表述.md"


def test_methodology_stub_exists() -> None:
    assert DOC.is_file()
    text = DOC.read_text(encoding="utf-8")
    assert "计算筛选" in text
    assert "实验验证" in text
    assert "S_final" in text
    assert "活力 ≥80%" in text or "活力 >=80%" in text
    assert "SI" in text
