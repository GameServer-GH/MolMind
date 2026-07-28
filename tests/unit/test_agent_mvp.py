"""Agent MVP：意图解析 + 会话流式 + 哈希对拍。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.app import app
from agent.intent import parse_intent
from services.pipeline import load_config, screen_sdf

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SDF = ROOT / "data" / "sample.sdf"


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("MOLMIND_LLM_MECHANISM", "0")
    monkeypatch.setenv("MOLMIND_LLM_NOMINATION_REVIEW", "0")
    monkeypatch.setenv("MOLMIND_LLM_CHAT", "0")
    return TestClient(app)


def test_parse_intent_csv_only() -> None:
    intent = parse_intent("帮我用sdf文件生成top10的候选分子提名清单（csv）")
    assert intent.want_csv is True
    assert intent.want_pdf is False
    assert intent.top_n == 10
    assert intent.wants_tools is True


def test_parse_intent_csv_and_pdf() -> None:
    intent = parse_intent("生成 top10 提名 csv，并给出机制与验证方案 pdf")
    assert intent.want_csv is True
    assert intent.want_pdf is True
    assert intent.top_n == 10
    assert intent.wants_tools is True


def test_parse_intent_top_over_limit() -> None:
    intent = parse_intent("生成 top60 提名 csv")
    assert intent.want_csv is True
    assert intent.requested_top_n == 60
    assert intent.top_n == 50
    assert intent.top_n_over_limit is True


def test_agent_top_over_limit_asks_confirm(client: TestClient) -> None:
    sid = client.post("/api/agent/sessions").json()["session_id"]
    with SAMPLE_SDF.open("rb") as fh:
        client.post(
            f"/api/agent/sessions/{sid}/upload",
            files={"file": ("sample.sdf", fh, "chemical/x-mdl-sdf")},
        )
    with client.stream(
        "POST",
        f"/api/agent/sessions/{sid}/message/stream",
        json={"text": "生成 top60 提名 csv"},
    ) as resp:
        events = [json.loads(line) for line in resp.iter_lines() if line]
    assert "tool_start" not in [e.get("type") for e in events]
    thinking = " ".join(e.get("text", "") for e in events if e.get("type") == "thinking")
    assert "60" in thinking and "50" in thinking
    text = next(e["text"] for e in events if e.get("type") == "assistant")
    assert "Top50" in text or "top50" in text.lower() or "Top 50" in text
    assert "吗" in text
    # Without LLM keyword table: restate an in-bound request to proceed.
    with client.stream(
        "POST",
        f"/api/agent/sessions/{sid}/message/stream",
        json={"text": "那就生成 top50 提名 csv"},
    ) as resp:
        events2 = [json.loads(line) for line in resp.iter_lines() if line]
    assert any(
        e.get("type") == "tool_start" and e.get("tool") == "score_and_rank" for e in events2
    )
    start = next(
        e for e in events2 if e.get("type") == "tool_start" and e.get("tool") == "score_and_rank"
    )
    assert start.get("args", {}).get("top_n") == 50


def test_registry_top_n_bounds_from_skill_yaml() -> None:
    from agent.registry import AgentRegistry

    reg = AgentRegistry()
    lo, hi = reg.resolve_top_n_bounds(skill_ids=["masld_nominate"])
    assert lo == 1
    assert hi == 50


def test_parse_intent_chat_without_tools() -> None:
    intent = parse_intent("你好，怎么用？")
    assert intent.wants_tools is False
    assert intent.want_csv is False
    assert intent.want_pdf is False


def test_parse_intent_mention_introduce() -> None:
    intent = parse_intent("介绍一下 @skill:masld_nominate")
    assert intent.mention_action == "introduce"
    assert len(intent.mentions) == 1
    assert intent.mentions[0].kind == "skill"
    assert intent.mentions[0].id == "masld_nominate"
    assert intent.wants_tools is False


def test_parse_intent_mention_defaults_introduce_not_keyword_invoke() -> None:
    # No verb table: parse_intent always safe-defaults to introduce; runtime LLM refines.
    intent = parse_intent("试用 /tool:score_and_rank")
    assert intent.mention_action == "introduce"
    assert intent.mentions[0].kind == "tool"
    assert intent.mentions[0].id == "score_and_rank"
    assert intent.wants_tools is False


def test_classify_mention_action_uses_llm_when_ready(monkeypatch) -> None:
    from agent.intent import MentionRef
    from agent.runtime.loop import AgentRuntime

    def fake_decide(**kwargs):
        assert "invoke" in kwargs["allowed"]
        return "invoke", "mocked"

    monkeypatch.setattr("agent.runtime.loop.llm_json_decision", fake_decide)
    rt = AgentRuntime()
    action, why = rt._classify_mention_action(
        "试用 /tool:score_and_rank",
        (MentionRef(kind="tool", id="score_and_rank", raw="/tool:score_and_rank"),),
    )
    assert action == "invoke"
    assert why == "mocked"


def test_agent_mention_introduce(client: TestClient) -> None:
    sid = client.post("/api/agent/sessions").json()["session_id"]
    with client.stream(
        "POST",
        f"/api/agent/sessions/{sid}/message/stream",
        json={"text": "介绍 @skill:masld_nominate"},
    ) as resp:
        assert resp.status_code == 200
        events = [json.loads(line) for line in resp.iter_lines() if line]
    types = [e.get("type") for e in events]
    assert "assistant" in types
    assert "done" in types
    assert "tool_start" not in types
    text = next(e["text"] for e in events if e.get("type") == "assistant")
    assert "masld_nominate" in text or "提名" in text


def test_agent_chat_without_sdf(client: TestClient) -> None:
    sid = client.post("/api/agent/sessions").json()["session_id"]
    with client.stream(
        "POST",
        f"/api/agent/sessions/{sid}/message/stream",
        json={"text": "你好，介绍一下你能做什么"},
    ) as resp:
        assert resp.status_code == 200
        events = [json.loads(line) for line in resp.iter_lines() if line]
    types = [e.get("type") for e in events]
    assert "assistant" in types
    assert "done" in types
    assert "error" not in types
    assert "tool_start" not in types
    text = next(e["text"] for e in events if e.get("type") == "assistant")
    assert "不调用筛选工具" not in text
    assert len(text) > 8


def test_agent_chat_answers_aromatic_question(client: TestClient, monkeypatch) -> None:
    sid = client.post("/api/agent/sessions").json()["session_id"]

    def fake_llm(self, session, text):
        assert "芳香" in text
        return "芳香环是平面共轭环系；筛选里会看结构特征，但不会单凭有芳香环改主榜。"

    monkeypatch.setattr(
        "agent.runtime.loop.AgentRuntime._llm_chat_reply", fake_llm
    )
    with client.stream(
        "POST",
        f"/api/agent/sessions/{sid}/message/stream",
        json={"text": "芳香环是啥"},
    ) as resp:
        events = [json.loads(line) for line in resp.iter_lines() if line]
    text = next(e["text"] for e in events if e.get("type") == "assistant")
    assert "芳香" in text
    assert "一般对话，暂不调用筛选工具" not in text
    assert "tool_start" not in [e.get("type") for e in events]


def test_agent_attachment_moves_into_message_keeps_session_sdf(client: TestClient) -> None:
    sid = client.post("/api/agent/sessions").json()["session_id"]
    with SAMPLE_SDF.open("rb") as fh:
        up = client.post(
            f"/api/agent/sessions/{sid}/upload",
            files={"file": ("sample.sdf", fh, "chemical/x-mdl-sdf")},
        )
    assert up.json()["sdf_ui_pending"] is True
    with client.stream(
        "POST",
        f"/api/agent/sessions/{sid}/message/stream",
        json={"text": "你好"},
    ) as resp:
        list(resp.iter_lines())
    detail = client.get(f"/api/agent/sessions/{sid}").json()
    assert detail["has_sdf"] is True
    assert detail["sdf_ui_pending"] is False
    user = next(m for m in detail["messages"] if m["role"] == "user")
    assert user["attachments"] and user["attachments"][0]["filename"] == "sample.sdf"
    # same session can still run nominate using retained SDF
    with client.stream(
        "POST",
        f"/api/agent/sessions/{sid}/message/stream",
        json={"text": "生成 top5 提名清单 csv"},
    ) as resp:
        events = [json.loads(line) for line in resp.iter_lines() if line]
    assert any(e.get("type") == "tool_start" and e.get("tool") == "score_and_rank" for e in events)


def test_agent_nominate_without_sdf_guides(client: TestClient) -> None:
    sid = client.post("/api/agent/sessions").json()["session_id"]
    with client.stream(
        "POST",
        f"/api/agent/sessions/{sid}/message/stream",
        json={"text": "生成 top10 提名清单 csv"},
    ) as resp:
        events = [json.loads(line) for line in resp.iter_lines() if line]
    types = [e.get("type") for e in events]
    assert "assistant" in types
    assert "error" not in types
    assert "tool_start" not in types
    text = next(e["text"] for e in events if e.get("type") == "assistant")
    assert "sdf" in text.lower() or "附件" in text


def test_agent_detach_sdf(client: TestClient) -> None:
    sid = client.post("/api/agent/sessions").json()["session_id"]
    with SAMPLE_SDF.open("rb") as fh:
        up = client.post(
            f"/api/agent/sessions/{sid}/upload",
            files={"file": ("sample.sdf", fh, "chemical/x-mdl-sdf")},
        )
    assert up.status_code == 200
    assert up.json()["has_sdf"] is True
    cleared = client.delete(f"/api/agent/sessions/{sid}/upload")
    assert cleared.status_code == 200
    assert cleared.json()["has_sdf"] is False
    detail = client.get(f"/api/agent/sessions/{sid}").json()
    assert not detail.get("has_sdf")
    assert not detail.get("sdf_filename")


def test_agent_session_upload_and_csv_stream(client: TestClient) -> None:
    created = client.post("/api/agent/sessions")
    assert created.status_code == 200
    sid = created.json()["session_id"]

    with SAMPLE_SDF.open("rb") as fh:
        up = client.post(
            f"/api/agent/sessions/{sid}/upload",
            files={"file": ("sample.sdf", fh, "chemical/x-mdl-sdf")},
        )
    assert up.status_code == 200

    with client.stream(
        "POST",
        f"/api/agent/sessions/{sid}/message/stream",
        json={"text": "帮我用sdf文件生成top10的候选分子提名清单（csv）"},
    ) as resp:
        assert resp.status_code == 200
        events = []
        for line in resp.iter_lines():
            if line:
                events.append(json.loads(line))

    types = [e.get("type") for e in events]
    assert "thinking" in types
    assert "plan" in types
    assert "tool_start" in types
    assert "card" in types
    assert "done" in types

    cards = [e["card"] for e in events if e.get("type") == "card"]
    assert cards and cards[0]["kind"] == "csv"
    dl = client.get(cards[0]["download_url"])
    assert dl.status_code == 200
    assert "text/csv" in dl.headers.get("content-type", "")
    assert len(dl.content) > 10

    assistant = next(e["text"] for e in events if e.get("type") == "assistant")
    assert "已按计划完成" not in assistant
    assert "| 排名 | 分子 |" in assistant
    assert "入选理由" in assistant


def test_agent_selection_hash_parity(client: TestClient) -> None:
    cfg = load_config(mode="auto", use_snapshot=True, allow_live=False)
    baseline = screen_sdf(SAMPLE_SDF, cfg=cfg, top_n=5, source_filename="sample.sdf")

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
        json={"text": "生成 top5 提名清单 csv", "top_n": 5},
    ) as resp:
        events = [json.loads(line) for line in resp.iter_lines() if line]

    ends = [
        e
        for e in events
        if e.get("type") == "tool_end" and e.get("tool") == "score_and_rank" and e.get("ok")
    ]
    assert ends
    assert ends[0]["digest"]["selection_sha256"] == baseline.selection_sha256
