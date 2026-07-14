"""services.mechanism — 机制假说（模板 / LLM 润色 → MD + PDF；不改排名）。"""

from services.mechanism.mechanism import build_mechanism_markdown, render_mechanism_markdown
from services.mechanism.pdf_export import markdown_to_pdf_bytes

__all__ = [
    "build_mechanism_markdown",
    "markdown_to_pdf_bytes",
    "render_mechanism_markdown",
]
