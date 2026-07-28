"""Content-Disposition / artifact download encoding."""

from __future__ import annotations

from agent.memory import Artifact, FileRunStore
from agent.runtime import loop as loop_mod
from apps.api.app import app
from apps.api.download_headers import content_disposition_attachment
from fastapi.testclient import TestClient
from plugins.molmind_core.scientific.mechanism.jobs import _safe_pdf_filename


def test_content_disposition_ascii_only_in_filename_param() -> None:
    header = content_disposition_attachment(
        "T001 TargetMol现货产品22966_mechanism_hypothesis.pdf"
    )
    # Starlette encodes headers as latin-1 — must not raise.
    header.encode("latin-1")
    assert "filename=" in header
    assert "filename*=UTF-8''" in header
    # Plain filename= must stay ASCII; UTF-8 form carries the original name.
    plain, _, starred = header.partition("filename*=")
    assert "现货" not in plain
    assert starred.startswith("UTF-8''")
    assert "mechanism_hypothesis.pdf" in header


def test_safe_pdf_filename_strips_non_ascii() -> None:
    name = _safe_pdf_filename("T001 TargetMol现货产品22966.sdf")
    assert name == "T001_TargetMol_22966_mechanism_hypothesis.pdf"
    name.encode("ascii")


def test_agent_artifact_download_with_chinese_filename(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MOLMIND_LLM_MECHANISM", "0")
    store = FileRunStore(root=tmp_path / "runs")
    monkeypatch.setattr(loop_mod, "_RUNTIME", loop_mod.AgentRuntime(store=store))

    client = TestClient(app)
    session = store.create()
    art = Artifact(
        artifact_id="deadbeef0001",
        kind="pdf",
        filename="T001 TargetMol现货产品22966_mechanism_hypothesis.pdf",
        title="机制与验证方案",
        subtitle="test",
        media_type="application/pdf",
        content=b"%PDF-1.4\n%test\n",
    )
    store.put_artifact(session, art)

    resp = client.get(
        f"/api/agent/sessions/{session.session_id}/artifacts/{art.artifact_id}/download"
    )
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF")
    cd = resp.headers.get("content-disposition") or ""
    cd.encode("latin-1")
    assert "attachment" in cd
