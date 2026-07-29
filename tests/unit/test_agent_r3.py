"""Agent R3：会话列表 / 事件回放 / Catalog 设置。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.app import app

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SDF = ROOT / "data" / "sample.sdf"


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("MOLMIND_LLM_MECHANISM", "0")
    monkeypatch.setenv("MOLMIND_LLM_NOMINATION_REVIEW", "0")
    monkeypatch.setenv("MOLMIND_LLM_CHAT", "0")
    # isolate FileRunStore
    from agent.memory import FileRunStore
    from agent.runtime import loop as loop_mod

    store = FileRunStore(root=tmp_path / "agent_runs")
    loop_mod._RUNTIME = None
    rt = loop_mod.AgentRuntime(store=store)
    loop_mod._RUNTIME = rt
    api_client = TestClient(app)
    api_client.headers.update({"X-MolMind-Client-ID": "browser_test_r3_0001"})
    return api_client


def test_list_sessions_and_events(client: TestClient) -> None:
    created = client.post("/api/agent/sessions").json()
    sid = created["session_id"]
    with SAMPLE_SDF.open("rb") as fh:
        client.post(
            f"/api/agent/sessions/{sid}/upload",
            files={"file": ("sample.sdf", fh, "chemical/x-mdl-sdf")},
        )
    with client.stream(
        "POST",
        f"/api/agent/sessions/{sid}/message/stream",
        json={"text": "生成 top3 提名清单 csv", "top_n": 3},
    ) as resp:
        assert resp.status_code == 200
        for _ in resp.iter_lines():
            pass

    listed = client.get("/api/agent/sessions").json()
    assert listed["count"] >= 1
    assert any(s["session_id"] == sid for s in listed["sessions"])
    assert listed["sessions"][0]["title"]

    detail = client.get(f"/api/agent/sessions/{sid}").json()
    assert detail["messages"]
    assert detail["artifacts"]

    events = client.get(f"/api/agent/sessions/{sid}/events").json()
    assert all(event.get("occurred_at") for event in events["events"])
    types = [e.get("type") for e in events["events"]]
    assert "thinking" in types
    assert "card" in types
    assert "done" in types
    completion = next(e["text"] for e in events["events"] if e.get("type") == "assistant")
    assert any(
        message.get("role") == "assistant" and message.get("text") == completion
        for message in detail["messages"]
    )


def test_session_preview_uses_latest_user_question(tmp_path) -> None:
    from agent.memory import FileRunStore

    store = FileRunStore(root=tmp_path / "agent_runs")
    session = store.create()
    session.messages = [
        {"role": "user", "text": "第一个问题"},
        {"role": "assistant", "text": "第一个回答"},
        {"role": "user", "text": "这是最新的问题"},
        {"role": "assistant", "text": "最新回答"},
    ]
    store.persist(session)

    assert store.list_sessions()[0]["preview"] == "这是最新的问题"


def test_settings_catalog_opt_in(client: TestClient) -> None:
    sid = client.post("/api/agent/sessions").json()["session_id"]
    settings = client.get(f"/api/agent/settings?session_id={sid}").json()
    assert "molmind-core" in settings["builtin_plugins"]
    assert settings["catalog_opt_in_only"] is True
    assert all(not c["installed"] for c in settings["catalog"])

    installed = client.post(
        f"/api/agent/sessions/{sid}/catalog/install",
        json={"plugin_id": "origene-mcp"},
    ).json()
    assert "origene-mcp" in installed["installed_catalog"]
    item = next(c for c in installed["settings"]["catalog"] if c["plugin_id"] == "origene-mcp")
    assert item["installed"] is True


def test_rename_and_delete_session(client: TestClient) -> None:
    sid = client.post("/api/agent/sessions").json()["session_id"]
    renamed = client.patch(
        f"/api/agent/sessions/{sid}",
        json={"title": "我的提名实验"},
    ).json()
    assert renamed["title"] == "我的提名实验"
    listed = client.get("/api/agent/sessions").json()
    assert any(s["session_id"] == sid and s["title"] == "我的提名实验" for s in listed["sessions"])

    deleted = client.delete(f"/api/agent/sessions/{sid}").json()
    assert deleted["deleted"] is True
    listed2 = client.get("/api/agent/sessions").json()
    assert all(s["session_id"] != sid for s in listed2["sessions"])
    assert client.get(f"/api/agent/sessions/{sid}").status_code == 404


def test_session_detail_exposes_fresh_summary_timestamps(client: TestClient) -> None:
    sid = client.post("/api/agent/sessions").json()["session_id"]

    detail = client.get(f"/api/agent/sessions/{sid}").json()

    assert detail["created_at"].endswith("Z")
    assert detail["updated_at"].endswith("Z")
    assert detail["event_seq"] == 0


def test_clear_sessions_removes_all_persisted_history(client: TestClient) -> None:
    first = client.post("/api/agent/sessions").json()["session_id"]
    second = client.post("/api/agent/sessions").json()["session_id"]

    cleared = client.delete("/api/agent/sessions")

    assert cleared.status_code == 200
    assert cleared.json()["deleted_count"] == 2
    assert client.get("/api/agent/sessions").json() == {"sessions": [], "count": 0}
    assert client.get(f"/api/agent/sessions/{first}").status_code == 404
    assert client.get(f"/api/agent/sessions/{second}").status_code == 404


def test_sessions_are_isolated_by_browser_client_id(client: TestClient) -> None:
    first_id = "browser_test_owner_a_0001"
    second_id = "browser_test_owner_b_0001"
    first_headers = {"X-MolMind-Client-ID": first_id}
    second_headers = {"X-MolMind-Client-ID": second_id}

    first_session = client.post("/api/agent/sessions", headers=first_headers).json()[
        "session_id"
    ]
    second_session = client.post("/api/agent/sessions", headers=second_headers).json()[
        "session_id"
    ]

    first_list = client.get("/api/agent/sessions", headers=first_headers).json()
    second_list = client.get("/api/agent/sessions", headers=second_headers).json()
    assert [item["session_id"] for item in first_list["sessions"]] == [first_session]
    assert [item["session_id"] for item in second_list["sessions"]] == [second_session]
    assert client.get(
        f"/api/agent/sessions/{second_session}", headers=first_headers
    ).status_code == 404

    cleared = client.delete("/api/agent/sessions", headers=first_headers).json()
    assert cleared["deleted_count"] == 1
    assert client.get(
        f"/api/agent/sessions/{second_session}", headers=second_headers
    ).status_code == 200


def test_legacy_unowned_sessions_are_not_visible_to_new_browser(
    client: TestClient,
) -> None:
    from agent.runtime import loop as loop_mod

    legacy = loop_mod._RUNTIME.create_session()
    listed = client.get("/api/agent/sessions").json()

    assert all(item["session_id"] != legacy.session_id for item in listed["sessions"])
    assert client.get(f"/api/agent/sessions/{legacy.session_id}").status_code == 404


def test_client_identity_validation_requires_existing_user_record(
    client: TestClient,
) -> None:
    target_headers = {"X-MolMind-Client-ID": "browser_switch_target_0001"}
    target_session = client.post("/api/agent/sessions", headers=target_headers).json()

    found = client.post(
        "/api/agent/clients/validate",
        json={"client_id": "browser_switch_target_0001"},
    )
    assert found.status_code == 200
    assert found.json() == {
        "client_id": "browser_switch_target_0001",
        "exists": True,
        "latest_session_id": target_session["session_id"],
    }
    assert target_session["session_id"]

    missing = client.post(
        "/api/agent/clients/validate",
        json={"client_id": "browser_switch_missing_0001"},
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == "未找到该用户记录"


def test_registered_client_can_be_restored_without_conversations(
    client: TestClient,
) -> None:
    empty_id = "browser_registered_empty_0001"
    registered = client.post(
        "/api/agent/clients/register",
        headers={"X-MolMind-Client-ID": empty_id},
    )
    assert registered.status_code == 200

    validated = client.post(
        "/api/agent/clients/validate",
        json={"client_id": empty_id},
    )
    assert validated.status_code == 200
    assert validated.json()["latest_session_id"] is None
