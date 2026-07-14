"""机制 Markdown → PDF。"""

from __future__ import annotations

from services.mechanism.pdf_export import markdown_to_pdf_bytes, normalize_for_pdf


def test_sanitize_drops_private_use_and_keeps_cjk() -> None:
    from services.mechanism.pdf_export import sanitize_pdf_text

    s = sanitize_pdf_text("活力 >=80% EC₅₀ 测试\ue000乱")
    assert "活力" in s
    assert "EC50" in s
    assert "\ue000" not in s


def test_markdown_to_pdf_contains_pdf_header() -> None:
    md = """# MolMind 机制与验证方案

共 1 个候选；**排名已冻结**。

## 候选 1. T001

### 机制假说

经 AMPK / PPARα 通路可能降脂。

### 实验验证协议（HepG2-FFA 双终点）

1. HepG2 + FFA
2. 活力 ≥80%
"""
    pdf = markdown_to_pdf_bytes(md)
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 500
