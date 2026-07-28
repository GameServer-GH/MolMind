"""HTTP download helpers (ASCII-safe Content-Disposition)."""

from __future__ import annotations

import re
from urllib.parse import quote


def content_disposition_attachment(filename: str) -> str:
    """Build Content-Disposition that stays latin-1 safe for Starlette/uvicorn.

    Non-ASCII names (e.g. Chinese SDF stems) must not appear in the plain
    ``filename=`` parameter — that raises UnicodeEncodeError and returns HTTP 500.
    """
    raw = (filename or "download.bin").replace('"', "").replace("\r", "").replace("\n", "")
    ascii_name = re.sub(r"[^\w.\-]+", "_", raw, flags=re.ASCII).strip("._") or "download.bin"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(raw)}"
