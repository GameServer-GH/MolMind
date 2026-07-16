"""机制 Markdown → PDF（交付用；字符集净化防乱码）。"""

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

_FONT = "STSong-Light"
_REGISTERED = False

_CJK_FONT_CANDIDATES = (
    # Explicit override for controlled/container deployments.
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    # macOS desktop runtime.
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
)

_UNICODE_FIXES: tuple[tuple[str, str], ...] = (
    ("EC₅₀", "EC50"),
    ("CC₅₀", "CC50"),
    ("IC₅₀", "IC50"),
    ("₀", "0"),
    ("₁", "1"),
    ("₂", "2"),
    ("₃", "3"),
    ("₄", "4"),
    ("₅", "5"),
    ("₆", "6"),
    ("₇", "7"),
    ("₈", "8"),
    ("₉", "9"),
    ("μM", "uM"),
    ("µM", "uM"),
    ("μg", "ug"),
    ("µg", "ug"),
    ("μmol", "umol"),
    ("≥", ">="),
    ("≤", "<="),
    ("×", "x"),
    ("·", "."),
    ("–", "-"),
    ("—", "-"),
    ("−", "-"),
    ("‐", "-"),
    ("‑", "-"),
    ("‒", "-"),
    ("―", "-"),
    ("∶", ":"),
    ("α", "alpha"),
    ("β", "beta"),
    ("γ", "gamma"),
    ("δ", "delta"),
    ("•", "-"),
    ("●", "-"),
    ("○", "-"),
    ("■", ""),
    ("□", ""),
    ("★", "*"),
    ("☆", "*"),
    ("∧", "且"),
    ("∨", "或"),
    ("°", " deg"),
)


def _ensure_font() -> None:
    global _FONT, _REGISTERED
    if _REGISTERED:
        return
    import os

    configured = str(os.environ.get("MOLMIND_CJK_FONT") or "").strip()
    candidates = ([configured] if configured else []) + list(_CJK_FONT_CANDIDATES)
    for raw_path in candidates:
        path = Path(raw_path)
        if not path.is_file():
            continue
        try:
            _FONT = "MolMindCJK"
            # TrueType/TTC fonts are embedded in the PDF. This avoids the former
            # non-embedded CID-font output whose Chinese text disappeared in
            # browser and Poppler renderers.
            pdfmetrics.registerFont(TTFont(_FONT, str(path)))
            _REGISTERED = True
            return
        except Exception:
            continue
    # Last-resort compatibility fallback; delivery images should install Noto CJK.
    _FONT = "STSong-Light"
    pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    _REGISTERED = True


def normalize_for_pdf(text: str) -> str:
    out = text or ""
    for src, dst in _UNICODE_FIXES:
        out = out.replace(src, dst)
    out = out.replace("\ufffd", "")
    return out


def sanitize_pdf_text(text: str) -> str:
    """仅保留 ASCII 可打印 + 常用中文，丢弃易导致 CID 乱码的字符。"""
    s = normalize_for_pdf(text)
    buf: list[str] = []
    for ch in s:
        o = ord(ch)
        if ch in "\n\r\t":
            buf.append(ch)
        elif 32 <= o <= 126:
            buf.append(ch)
        elif 0x4E00 <= o <= 0x9FFF:  # CJK Unified
            buf.append(ch)
        elif ch in "、。，《》【】（）「」『』：；！？":
            buf.append(ch)
        # 其余丢弃（含私用区、特殊符号、残留繁体怪字）
    return "".join(buf)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _inline_md(text: str) -> str:
    t = _escape(sanitize_pdf_text(text))
    t = re.sub(r"`([^`]+)`", r"<font face='Courier'><font size='9'>\1</font></font>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)
    return t


def markdown_to_pdf_bytes(md: str, *, title: str = "MolMind 机制与验证方案") -> bytes:
    _ensure_font()
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=title,
        author="MolMind",
    )
    styles = {
        "h1": ParagraphStyle(
            "mh1", fontName=_FONT, fontSize=16, leading=22, spaceAfter=10, spaceBefore=4
        ),
        "h2": ParagraphStyle(
            "mh2", fontName=_FONT, fontSize=13, leading=18, spaceAfter=8, spaceBefore=12
        ),
        "h3": ParagraphStyle(
            "mh3", fontName=_FONT, fontSize=11, leading=16, spaceAfter=6, spaceBefore=8
        ),
        "body": ParagraphStyle(
            "mbody", fontName=_FONT, fontSize=10, leading=15, spaceAfter=4
        ),
        "quote": ParagraphStyle(
            "mquote",
            fontName=_FONT,
            fontSize=9,
            leading=13,
            textColor="#444444",
            leftIndent=8,
            spaceAfter=6,
        ),
        "li": ParagraphStyle(
            "mli", fontName=_FONT, fontSize=10, leading=14, leftIndent=12, spaceAfter=2
        ),
    }

    story: list = []
    for raw in sanitize_pdf_text(md or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            story.append(Spacer(1, 4))
            continue
        if line.strip().startswith("<!--"):
            continue
        if line.strip() in {"---", "***", "___"}:
            story.append(HRFlowable(width="100%", thickness=0.4, color="#999999"))
            story.append(Spacer(1, 6))
            continue
        if line.startswith("# "):
            story.append(Paragraph(_inline_md(line[2:].strip()), styles["h1"]))
            continue
        if line.startswith("## "):
            story.append(Paragraph(_inline_md(line[3:].strip()), styles["h2"]))
            continue
        if line.startswith("### "):
            story.append(Paragraph(_inline_md(line[4:].strip()), styles["h3"]))
            continue
        if line.lstrip().startswith("> "):
            story.append(Paragraph(_inline_md(line.lstrip()[2:].strip()), styles["quote"]))
            continue
        if re.match(r"^\s*[-*]\s+", line) or re.match(r"^\s*\d+\.\s+", line):
            text = re.sub(r"^\s*[-*]\s+", "", line)
            text = re.sub(r"^\s*\d+\.\s+", "", text)
            story.append(Paragraph("- " + _inline_md(text.strip()), styles["li"]))
            continue
        story.append(Paragraph(_inline_md(line.strip()), styles["body"]))

    if not story:
        story.append(Paragraph(_escape(sanitize_pdf_text(title)), styles["h1"]))
    def invariant_canvas(*args, **kwargs):
        # ReportLab 默认写入当前时间和随机文档 ID；固定元数据后，同一 Markdown
        # 在不同运行中生成逐字节一致的交付物。
        kwargs["invariant"] = 1
        return Canvas(*args, **kwargs)

    doc.build(story, canvasmaker=invariant_canvas)
    return buf.getvalue()
