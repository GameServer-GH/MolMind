"""apps.api：FastAPI TestClient；支持 Quality-Max / online / offline。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.app import app
from services.pipeline.runner import TOP_N_MAX, TOP_N_MIN

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SDF = ROOT / "data" / "sample.sdf"
WEB_INDEX = ROOT / "apps" / "web" / "static" / "index.html"


@pytest.fixture
def client(monkeypatch) -> TestClient:
    # API 测例用模板机制，避免真打 DeepSeek
    monkeypatch.setenv("MOLMIND_LLM_MECHANISM", "0")
    return TestClient(app)


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["mode"] == "auto"
    assert data["top_n_min"] == TOP_N_MIN
    assert data["top_n_max"] == TOP_N_MAX


def test_screen_upload(client: TestClient) -> None:
    with SAMPLE_SDF.open("rb") as fh:
        resp = client.post(
            "/api/screen?top=5&mode=auto&use_snapshot=true",
            files={"file": ("sample.sdf", fh, "chemical/x-mdl-sdf")},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["mode"] == "auto"
    assert data["summary"]["use_snapshot"] is True
    diagnostics = data["summary"]["diagnostics"]
    assert diagnostics["engineering_pass"] is True
    assert diagnostics["scientific_validation_status"].startswith("not_available")
    assert data["hepg2_ffa_resources"]["ranking_effect"] == "none"
    assert data["hepg2_ffa_resources"]["dual_endpoint_model_available"] is False
    assert data["hepg2_ffa_resources"]["resource_counts"]["total"] == 6
    assert len(data["rows"]) >= 1
    assert all(row["eligibility_status"] == "eligible" for row in data["rows"])
    csv_ids = []
    import csv
    import io

    csv_ids = [row["molecule_id"] for row in csv.DictReader(io.StringIO(data["csv"]))]
    assert csv_ids == [row["molecule_id"] for row in data["rows"]]
    assert "SI" not in data["csv"]
    assert "EC50" not in data["csv"]
    assert isinstance(data["logs"], list)
    assert len(data["logs"]) >= 6
    assert "message" in data["logs"][0] and "lang" in data["logs"][0]
    langs = {e["lang"] for e in data["logs"]}
    assert "zh" in langs and "en" in langs
    # 中英分行：相邻成对出现
    assert data["logs"][0]["lang"] == "zh"
    assert data["logs"][1]["lang"] == "en"
    job_id = data.get("mechanism_job_id") or data["summary"].get("mechanism_job_id")
    assert job_id
    # 异步任务：轮询至 ready（模板路径应很快）
    import time

    payload = None
    for _ in range(60):
        st = client.get(f"/api/mechanism/{job_id}?include_payload=true")
        assert st.status_code == 200
        payload = st.json()
        if payload["status"] in {"ready", "error"}:
            break
        time.sleep(0.25)
    assert payload is not None
    assert payload["status"] == "ready", payload
    assert payload.get("mechanism_pdf_base64")
    assert payload.get("mechanism_html", "").startswith("<!doctype html>")
    assert payload.get("pdf_renderer") in {"html_chromium", "reportlab_fallback"}
    preview = client.get(f"/api/mechanism/{job_id}/preview")
    assert preview.status_code == 200
    assert "HepG2-FFA" in preview.text
    raw = __import__("base64").b64decode(payload["mechanism_pdf_base64"])
    assert raw[:4] == b"%PDF"
    assert "机制" in payload.get("mechanism_md", "") or "MolMind" in payload.get("mechanism_md", "")


def test_screen_mode_offline(client: TestClient) -> None:
    with SAMPLE_SDF.open("rb") as fh:
        resp = client.post(
            "/api/screen?top=3&mode=offline",
            files={"file": ("sample.sdf", fh, "chemical/x-mdl-sdf")},
        )
    assert resp.status_code == 200
    assert resp.json()["summary"]["mode"] == "offline"


def test_screen_use_snapshot_off(client: TestClient) -> None:
    with SAMPLE_SDF.open("rb") as fh:
        resp = client.post(
            "/api/screen?top=3&mode=offline&use_snapshot=false",
            files={"file": ("sample.sdf", fh, "chemical/x-mdl-sdf")},
        )
    assert resp.status_code == 200
    assert resp.json()["summary"]["use_snapshot"] is False


def test_quality_max_cannot_disable_frozen_snapshot(client: TestClient) -> None:
    with SAMPLE_SDF.open("rb") as fh:
        resp = client.post(
            "/api/screen?top=3&mode=auto&use_snapshot=false",
            files={"file": ("sample.sdf", fh, "chemical/x-mdl-sdf")},
        )
    assert resp.status_code == 200
    assert resp.json()["summary"]["use_snapshot"] is True


def test_screen_top_bounds(client: TestClient) -> None:
    with SAMPLE_SDF.open("rb") as fh:
        resp = client.post(
            f"/api/screen?top={TOP_N_MAX + 1}",
            files={"file": ("sample.sdf", fh, "chemical/x-mdl-sdf")},
        )
    assert resp.status_code == 400


def test_screen_stream(client: TestClient) -> None:
    with SAMPLE_SDF.open("rb") as fh:
        with client.stream(
            "POST",
            "/api/screen/stream?top=3&mode=auto",
            files={"file": ("sample.sdf", fh, "chemical/x-mdl-sdf")},
        ) as resp:
            assert resp.status_code == 200
            text = "".join(resp.iter_text())
    frames = [json.loads(line) for line in text.strip().splitlines() if line.strip()]
    assert any(f.get("type") == "log" for f in frames)
    result = next(f for f in frames if f.get("type") == "result")
    assert result["summary"]["mode"] == "auto"
    assert len(result["rows"]) >= 1


def test_web_mode_selector_present() -> None:
    html = WEB_INDEX.read_text(encoding="utf-8")
    assert "Quality-Max" in html
    assert "运行模式" in html
    assert "使用快照" in html
    assert 'id="useSnapshot"' in html
    assert 'data-mode="auto"' in html
    assert 'data-mode="online"' in html
    assert 'data-mode="offline"' in html
    assert "historyBtn" in html
    assert "下载运行日志" in html
    js = (ROOT / "apps" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "row.eligibility_status" in js
