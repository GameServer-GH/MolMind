"""Render the mechanism HTML template with a local headless Chromium browser."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class BrowserPdfUnavailable(RuntimeError):
    pass


def find_chromium() -> str | None:
    configured = str(os.environ.get("MOLMIND_CHROME_BIN") or "").strip()
    candidates = [
        configured,
        shutil.which("chromium") or "",
        shutil.which("chromium-browser") or "",
        shutil.which("google-chrome") or "",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    return next((path for path in candidates if path and Path(path).is_file()), None)


def html_to_pdf_bytes(html: str, *, timeout_sec: float = 45.0) -> bytes:
    chromium = find_chromium()
    if not chromium:
        raise BrowserPdfUnavailable("headless Chromium executable not found")
    with tempfile.TemporaryDirectory(prefix="molmind-html-pdf-") as raw_dir:
        tmp = Path(raw_dir)
        html_path = tmp / "report.html"
        pdf_path = tmp / "report.pdf"
        html_path.write_text(html, encoding="utf-8")
        cmd = [
            chromium,
            "--headless=new",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-pdf-header-footer",
            "--run-all-compositor-stages-before-draw",
            f"--print-to-pdf={pdf_path}",
            html_path.as_uri(),
        ]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            cmd.insert(1, "--no-sandbox")
        try:
            completed = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BrowserPdfUnavailable(str(exc)) from exc
        if completed.returncode != 0 or not pdf_path.is_file():
            detail = (completed.stderr or completed.stdout or "unknown Chromium failure")[-800:]
            raise BrowserPdfUnavailable(detail)
        payload = pdf_path.read_bytes()
        if not payload.startswith(b"%PDF"):
            raise BrowserPdfUnavailable("Chromium did not produce a valid PDF")
        return payload


__all__ = ["BrowserPdfUnavailable", "find_chromium", "html_to_pdf_bytes"]
