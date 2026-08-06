"""Shared helpers for Agent turn attachments (type allow-list and media types)."""

from __future__ import annotations

from typing import Any

from agent.memory.blob_store import (
    ALLOWED_ATTACHMENT_EXTENSIONS,
    attachment_kind_for_filename,
    is_allowed_attachment_filename,
)

__all__ = [
    "ALLOWED_ATTACHMENT_EXTENSIONS",
    "attachment_kind_for_filename",
    "format_attachment_context",
    "guess_media_type",
    "is_allowed_attachment_filename",
    "summarize_attachment_for_context",
]

_TEXT_EXTENSIONS = (".txt", ".md", ".csv", ".tsv", ".json")
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def guess_media_type(filename: str, fallback: str = "application/octet-stream") -> str:
    name = (filename or "").lower()
    mapping = {
        ".sdf": "chemical/x-mdl-sdfile",
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".csv": "text/csv",
        ".tsv": "text/tab-separated-values",
        ".json": "application/json",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    for ext, media in mapping.items():
        if name.endswith(ext):
            return media
    return fallback or "application/octet-stream"


def summarize_attachment_for_context(
    metadata: dict[str, Any],
    content: bytes,
    *,
    excerpt_chars: int = 2000,
) -> dict[str, Any]:
    """Build a bounded, LLM-safe summary for one turn attachment.

    SDF remains a library bind (handled elsewhere). Text-like documents get a
    clipped excerpt; PDF/images/office docs only contribute metadata notes so
    the planner cannot treat them as ranking inputs.
    """
    filename = str(metadata.get("filename") or "attachment")
    kind = str(metadata.get("kind") or attachment_kind_for_filename(filename) or "binary")
    media_type = str(metadata.get("media_type") or guess_media_type(filename))
    size = int(metadata.get("size") or len(content) or 0)
    lower = filename.lower()
    summary: dict[str, Any] = {
        "attachment_id": str(metadata.get("attachment_id") or ""),
        "filename": filename,
        "kind": kind,
        "media_type": media_type,
        "size": size,
        "usable_for_ranking": False,
        "excerpt": "",
        "note": "",
    }
    if kind == "sdf" or lower.endswith(".sdf"):
        summary["usable_for_ranking"] = True
        summary["note"] = "SDF 化合物库附件；仅本会话可用于 score_and_rank。"
        return summary
    if lower.endswith(_TEXT_EXTENSIONS):
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = content.decode("utf-8", errors="replace")
        clipped = text.strip()
        if len(clipped) > max(200, int(excerpt_chars)):
            clipped = clipped[: max(0, int(excerpt_chars) - 1)].rstrip() + "…"
        summary["excerpt"] = clipped
        summary["note"] = "文本文档摘录（非 SDF，不可用于筛选排名）。"
        return summary
    if kind == "pdf" or lower.endswith(".pdf"):
        summary["note"] = "PDF 附件：已暂存但未解析正文；不可用于筛选排名。"
        return summary
    if kind == "image" or lower.endswith(_IMAGE_EXTENSIONS):
        summary["note"] = "图片附件：已暂存但未做视觉解析；不可用于筛选排名。"
        return summary
    summary["note"] = "办公文档/二进制附件：已暂存；本轮仅作上下文提示，不可用于筛选排名。"
    return summary


def format_attachment_context(summaries: list[dict[str, Any]], *, limit: int = 6) -> str:
    """Render attachment summaries for system/planner prompts."""
    items = [item for item in summaries if isinstance(item, dict)][: max(0, int(limit))]
    if not items:
        return ""
    lines: list[str] = ["本轮非 SDF 附件上下文（不可当作已完成筛选结果）："]
    for item in items:
        filename = str(item.get("filename") or "attachment")
        kind = str(item.get("kind") or "binary")
        note = str(item.get("note") or "").strip()
        lines.append(f"- {filename}（kind={kind}）{('：' + note) if note else ''}")
        excerpt = str(item.get("excerpt") or "").strip()
        if excerpt:
            lines.append(f"  摘录：{excerpt[:800]}")
    return "\n".join(lines)
