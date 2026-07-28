"""apps.api：FastAPI TestClient；Quality-Max + 快照/联网开关。"""

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
    monkeypatch.setenv("MOLMIND_LLM_MECHANISM", "0")
    monkeypatch.setenv("MOLMIND_LLM_NOMINATION_REVIEW", "0")
    return TestClient(app)


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["version"] == app.version
    assert data["version"] != "unknown"
    assert data["build"]
    assert data["mode"] == "auto"
    assert data["quality_max"] is True
    assert data["use_snapshot"] is True
    assert data["allow_live"] is False
    assert data["top_n_min"] == TOP_N_MIN
    assert data["top_n_max"] == TOP_N_MAX


def test_screen_upload(client: TestClient) -> None:
    with SAMPLE_SDF.open("rb") as fh:
        resp = client.post(
            "/api/screen?top=5&use_snapshot=true&allow_live=false",
            files={"file": ("sample.sdf", fh, "chemical/x-mdl-sdf")},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["mode"] == "auto"
    assert data["summary"]["use_snapshot"] is True
    assert data["summary"]["allow_live"] is False
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
    assert data["logs"][0]["lang"] == "zh"
    assert data["logs"][1]["lang"] == "en"
    job_id = data.get("mechanism_job_id") or data["summary"].get("mechanism_job_id")
    assert job_id
    import time

    payload = None
    for _ in range(60):
        st = client.get(f"/api/mechanism/{job_id}?include_payload=true")
        assert st.status_code == 200
        payload = st.json()
        if payload["status"] in {"ready", "error"}:
            break
        time.sleep(0.05)
    assert payload is not None
    assert payload["status"] == "ready"
    preview = client.get(f"/api/mechanism/{job_id}/preview")
    assert preview.status_code == 200
    assert "HepG2-FFA" in preview.text
    raw = __import__("base64").b64decode(payload["mechanism_pdf_base64"])
    assert raw[:4] == b"%PDF"
    assert "机制" in payload.get("mechanism_md", "") or "MolMind" in payload.get("mechanism_md", "")


def test_legacy_mode_online_enables_allow_live(client: TestClient) -> None:
    with SAMPLE_SDF.open("rb") as fh:
        resp = client.post(
            "/api/screen?top=3&mode=online",
            files={"file": ("sample.sdf", fh, "chemical/x-mdl-sdf")},
        )
    assert resp.status_code == 200
    summary = resp.json()["summary"]
    assert summary["mode"] == "auto"
    assert summary["allow_live"] is True


def test_screen_use_snapshot_off(client: TestClient) -> None:
    with SAMPLE_SDF.open("rb") as fh:
        resp = client.post(
            "/api/screen?top=3&use_snapshot=false",
            files={"file": ("sample.sdf", fh, "chemical/x-mdl-sdf")},
        )
    assert resp.status_code == 200
    assert resp.json()["summary"]["use_snapshot"] is False


def test_screen_top_bounds(client: TestClient) -> None:
    with SAMPLE_SDF.open("rb") as fh:
        resp = client.post(
            f"/api/screen?top={TOP_N_MAX + 1}",
            files={"file": ("sample.sdf", fh, "chemical/x-mdl-sdf")},
        )
    assert resp.status_code == 400


def test_screen_stream_ndjson(client: TestClient) -> None:
    with SAMPLE_SDF.open("rb") as fh:
        resp = client.post(
            "/api/screen/stream?top=3&allow_live=false",
            files={"file": ("sample.sdf", fh, "chemical/x-mdl-sdf")},
        )
    assert resp.status_code == 200
    lines = [json.loads(line) for line in resp.text.strip().splitlines() if line.strip()]
    result = next(evt for evt in lines if evt.get("type") == "result")
    assert result["summary"]["mode"] == "auto"
    assert result["summary"]["allow_live"] is False
    assert len(result["rows"]) >= 1


def test_screen_nomination_review_gates_mechanism(client: TestClient) -> None:
    with SAMPLE_SDF.open("rb") as fh:
        resp = client.post(
            "/api/screen?top=3&nomination_review=true&allow_live=false",
            files={"file": ("sample.sdf", fh, "chemical/x-mdl-sdf")},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["nomination_review"] is True
    assert data["summary"]["review_pending"] is True
    assert not (data.get("mechanism_job_id") or "")
    assert data["interactive_review"]["enabled"] is True
    run_id = data["summary"]["run_id"]
    assert run_id

    applied = client.post(
        "/api/screen/apply-review",
        json={"run_id": run_id, "selected_proposal_ids": []},
    )
    assert applied.status_code == 200
    body = applied.json()
    assert body["summary"]["review_pending"] is False
    job_id = body.get("mechanism_job_id") or body["summary"].get("mechanism_job_id")
    assert job_id
    assert body["csv"]
    assert [row["molecule_id"] for row in body["rows"]] == [
        row["molecule_id"] for row in data["rows"]
    ]


def test_screen_stream_review_pending(client: TestClient) -> None:
    with SAMPLE_SDF.open("rb") as fh:
        resp = client.post(
            "/api/screen/stream?top=3&nomination_review=true&allow_live=false",
            files={"file": ("sample.sdf", fh, "chemical/x-mdl-sdf")},
        )
    assert resp.status_code == 200
    lines = [json.loads(line) for line in resp.text.strip().splitlines() if line.strip()]
    assert not any(evt.get("type") == "result" for evt in lines)
    pending = next(evt for evt in lines if evt.get("type") == "review_pending")
    assert pending["summary"]["review_pending"] is True
    assert not (pending.get("mechanism_job_id") or "")
    assert pending["interactive_review"]["enabled"] is True


def test_web_runtime_switches_present() -> None:
    html = WEB_INDEX.read_text(encoding="utf-8")
    assert "Quality-Max" in html
    assert "使用快照" in html
    assert "联网补证据" in html
    assert "LLM+人工复核" in html
    assert 'id="useSnapshot"' in html
    assert 'id="allowLive"' in html
    assert 'id="allowLive" type="checkbox" checked' not in html
    assert 'id="nominationReview"' in html
    assert "historyBtn" in html
    assert "下载运行日志" in html
    assert "review-modal-panel" in html
    assert "reviewModal" in html
    assert "确认" in html
    assert "reviewSkipBtn" not in html
    assert "应用已选复核" not in html
    js = (ROOT / "apps" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "row.eligibility_status" in js
    assert "allowLiveEnabled" in js
    assert "nominationReviewEnabled" in js
    assert "actionableReviewProposals" in js
    assert "hasReviewContent" in js
    assert "review_pending" in js
    assert "applyInteractiveReview" in js
    assert "max-h-[42vh]" not in js


def test_screen_without_nomination_review_starts_mechanism(client: TestClient) -> None:
    with SAMPLE_SDF.open("rb") as fh:
        resp = client.post(
            "/api/screen?top=3&nomination_review=false&allow_live=false",
            files={"file": ("sample.sdf", fh, "chemical/x-mdl-sdf")},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["nomination_review"] is False
    assert data["summary"]["review_pending"] is False
    assert data["interactive_review"]["enabled"] is False
    assert data["interactive_review"]["requires_human_confirm"] is False
    job_id = data.get("mechanism_job_id") or data["summary"].get("mechanism_job_id")
    assert job_id
