"""Agent MVP：意图解析 + 会话流式 + 哈希对拍。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.app import app
from agent.intent import (
    extract_ranking_positions,
    parse_intent,
    ranking_position_subject_fallback,
)
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


def test_parse_intent_only_proposes_tool_shape_for_ambiguous_top1_text() -> None:
    # Structural parsing is deliberately not the final dialog-act decision.
    intent = parse_intent("为啥top1是T19959")
    assert intent.wants_tools is True
    assert intent.want_csv is True
    assert intent.explain_ranking is False


def test_extract_ranking_positions_preserves_each_named_rank() -> None:
    assert extract_ranking_positions("解释一下 top4 和 top5") == (4, 5)
    assert extract_ranking_positions("请说明第4名、Top 5、top4") == (4, 5)
    assert ranking_position_subject_fallback("介绍一下排名top5的分子") is True
    assert ranking_position_subject_fallback("介绍一下 Top5") is False


def test_runtime_llm_classifies_ranking_followup_before_tools(
    monkeypatch, tmp_path
) -> None:
    from agent.memory import FileRunStore
    from agent.runtime.loop import AgentRuntime

    def fake_decide(**kwargs):
        assert kwargs["purpose"] == "agent_chat"
        assert "execute_tools" in kwargs["allowed"]
        assert "top1" in kwargs["user"].lower()
        return "explain_ranking", "asks why prior rank"

    monkeypatch.setattr("agent.runtime.loop.llm_json_decision", fake_decide)
    rt = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    session = rt.create_session()
    intent = parse_intent("为啥top1是T19959")
    action, why = rt._classify_request_action(
        session, "为啥top1是T19959", intent
    )
    assert action == "explain_ranking"
    assert why == "asks why prior rank"


def test_runtime_llm_classifies_top_n_explanation_with_frozen_count(
    monkeypatch, tmp_path
) -> None:
    from types import SimpleNamespace

    from agent.memory import FileRunStore
    from agent.runtime.loop import AgentRuntime

    def fake_decide(**kwargs):
        assert "最近冻结主榜数量：15" in kwargs["user"]
        assert "解释/说明" in kwargs["system"]
        return "explain_ranking", "explains frozen Top15"

    monkeypatch.setattr("agent.runtime.loop.llm_json_decision", fake_decide)
    rt = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    session = rt.create_session()
    session.last_result = SimpleNamespace(top_molecules=[object()] * 15)
    intent = parse_intent("解释一下 Top15")
    action, _ = rt._classify_request_action(session, "解释一下 Top15", intent)
    assert action == "explain_ranking"


@pytest.mark.parametrize(
    "text",
    [
        "为啥top1是T19959",
        "为什么 Top 1 是 T19959？",
        "T19959 怎么会排第一名",
        "Top1 是 T19959 吗？",
        "解释一下 Top15",
    ],
)
def test_runtime_offline_fallback_keeps_ranking_question_read_only(
    text: str, monkeypatch, tmp_path
) -> None:
    from agent.memory import FileRunStore
    from agent.runtime.loop import AgentRuntime

    monkeypatch.setattr(
        "agent.runtime.loop.llm_json_decision",
        lambda **kwargs: ("execute_tools", "llm_not_ready"),
    )
    rt = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    session = rt.create_session()
    intent = parse_intent(text)
    action, why = rt._classify_request_action(session, text, intent)
    assert action == "explain_ranking"
    assert "structural_question_fallback" in why


def test_parse_intent_explicit_top1_generation_still_runs_tools() -> None:
    intent = parse_intent("请生成 top1 候选清单 csv")
    assert intent.wants_tools is True
    assert intent.want_csv is True
    assert intent.top_n == 1
    assert intent.explain_ranking is False


def test_parse_intent_reserve_and_submission_bundle() -> None:
    reserve = parse_intent("导出 nomination_reserve.csv")
    assert reserve.want_csv is False
    assert reserve.want_reserve is True
    assert reserve.want_bundle is False

    both = parse_intent("导出 Top10 和候补名单")
    assert both.want_csv is True
    assert both.want_reserve is True
    assert both.want_bundle is False

    bundle = parse_intent("生成结果包，包含候选清单和候补名单")
    assert bundle.want_csv is True
    assert bundle.want_reserve is True
    assert bundle.want_bundle is True
    assert "masld_export_bundle" in bundle.skill_ids


def test_agent_exports_frozen_reserve_and_submission_bundle(monkeypatch, tmp_path) -> None:
    import io
    import json
    import zipfile
    from types import SimpleNamespace

    from agent.memory import FileRunStore
    from agent.runtime.loop import AgentRuntime

    monkeypatch.setattr(
        "agent.runtime.loop.llm_json_decision",
        lambda **kwargs: ("execute_tools", "explicit export"),
    )
    config = SimpleNamespace(
        reserve_n=20,
        config_hash="config-hash",
        mode="auto",
        degraded_channels=[],
    )
    molecule = SimpleNamespace(
        molecule_id="P01",
        selection_score=0.6,
        final_score=0.6,
        lipid_score=0.4,
        tox_risk=0.2,
        novelty_score=0.8,
        lipid_rationale="药效团: aromatic ring",
        selection_tier="similarity_strict",
        screening_concentration_um=10.0,
    )
    reserve_molecule = SimpleNamespace(
        molecule_id="R01",
        selection_score=0.5,
        final_score=0.5,
        lipid_score=0.39,
        tox_risk=0.21,
        novelty_score=0.7,
        lipid_rationale="药效团: aromatic ring",
        selection_tier="similarity_strict",
        screening_concentration_um=10.0,
    )
    result = SimpleNamespace(
        run_id="mm-frozen-run",
        input_sha256="input-hash",
        selection_sha256="primary-selection",
        reserve_selection_sha256="reserve-selection",
        config=config,
        top_molecules=[molecule],
        reserve_molecules=[reserve_molecule],
        source_filename="library.sdf",
        to_csv_text=lambda: "molecule_id,nomination_tier\nP01,primary\n",
        to_reserve_csv_text=lambda: "molecule_id,nomination_tier,reserve_rank\nR01,reserve,1\n",
    )
    rt = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    session = rt.create_session()
    session.sdf_filename = "library.sdf"
    session.last_result = result

    reserve_events = list(rt.handle_message(session, "导出 nomination_reserve.csv"))
    assert "score_and_rank" not in [event.get("tool") for event in reserve_events]
    reserve_card = next(
        event["card"]
        for event in reserve_events
        if event.get("type") == "card" and event["card"]["filename"].endswith("_nomination_reserve.csv")
    )
    reserve_artifact = session.artifacts[reserve_card["artifact_id"]]
    assert reserve_artifact.content.startswith(b"\xef\xbb\xbf")
    assert "reserve-sele" in reserve_artifact.subtitle

    primary_events = list(rt._export_primary_only(session))
    primary_card = next(event["card"] for event in primary_events if event.get("type") == "card")
    assert primary_card["title"] == "候选分子清单：Top 1"
    assert primary_card["filename"] == "library_nomination_top1.csv"
    artifact_count = len(session.artifacts)
    reused_events = list(rt._export_primary_only(session))
    reused_end = next(event for event in reused_events if event.get("type") == "tool_end")
    assert reused_end["digest"]["reused"] is True
    assert len(session.artifacts) == artifact_count

    bundle_events = list(rt.handle_message(session, "生成结果包，包含候选清单和候补名单"))
    bundle_card = next(
        event["card"]
        for event in bundle_events
        if event.get("type") == "card" and event["card"]["kind"] == "bundle"
    )
    bundle_artifact = session.artifacts[bundle_card["artifact_id"]]
    assert bundle_card["title"] == "结果归档包：候选清单 + 候补 + 审计"
    assert bundle_card["filename"] == "library_results_bundle.zip"
    with zipfile.ZipFile(io.BytesIO(bundle_artifact.content)) as zf:
        assert "library_nomination_top1.csv" in zf.namelist()
        assert "library_nomination_reserve.csv" in zf.namelist()
        manifest = json.loads(zf.read("library_submission_manifest.json"))
    assert manifest["run_id"] == "mm-frozen-run"
    assert manifest["input_sha256"] == "input-hash"
    assert manifest["config_hash"] == "config-hash"
    assert manifest["primary"]["selection_sha256"] == "primary-selection"
    assert manifest["reserve"]["selection_sha256"] == "reserve-selection"


def test_agent_ranking_followup_answers_frozen_result_without_tool(
    monkeypatch, tmp_path
) -> None:
    from types import SimpleNamespace

    from agent.memory import FileRunStore
    from agent.runtime.loop import AgentRuntime

    monkeypatch.setattr(
        "agent.runtime.loop.llm_json_decision",
        lambda **kwargs: ("explain_ranking", "followup explanation"),
    )
    top1 = SimpleNamespace(
        molecule_id="T19959",
        selection_score=0.504,
        competition_scoring_version="organizer-relative-effect-novelty-v1",
        final_score=0.5,
        lipid_score=0.399,
        tox_risk=0.345,
        novelty_score=0.8,
        effect_rank=2,
        novelty_rank=3,
        lipid_rationale="药效团: carboxylic acid, aromatic ring",
        selection_tier="similarity_strict",
    )
    top2 = SimpleNamespace(
        molecule_id="T27832",
        selection_score=0.426,
        competition_scoring_version="organizer-relative-effect-novelty-v1",
        final_score=0.4,
        lipid_score=0.379,
        tox_risk=0.295,
        novelty_score=0.7,
        effect_rank=4,
        novelty_rank=5,
        lipid_rationale="药效团: carboxylic acid",
        selection_tier="similarity_strict",
    )
    result = SimpleNamespace(
        run_id="mm-existing",
        top_molecules=[top1, top2],
        reserve_molecules=[],
        scored_molecules=[],
    )
    rt = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    session = rt.create_session()
    session.last_result = result

    events = list(rt.handle_message(session, "为啥top1是T19959"))
    assert "tool_start" not in [event.get("type") for event in events]
    assert session.top_n == 10
    assert session.last_result is result
    reply = next(
        event["text"] for event in events if event.get("type") == "assistant"
    )
    assert "T19959" in reply
    assert "0.504" in reply
    assert "不会重新筛选" in reply


@pytest.mark.parametrize("text", ["解释一下top4和top5", "不是让你解释top4和top5嘛"])
def test_agent_ranking_followup_explains_each_named_rank(
    text: str, monkeypatch, tmp_path
) -> None:
    from types import SimpleNamespace

    from agent.memory import FileRunStore
    from agent.runtime.loop import AgentRuntime

    monkeypatch.setattr(
        "agent.runtime.loop.llm_json_decision",
        lambda **_kwargs: ("explain_ranking", "frozen ranking followup"),
    )

    def molecule(rank: int) -> SimpleNamespace:
        return SimpleNamespace(
            molecule_id=f"T{rank}",
            selection_score=0.50 - rank / 100,
            competition_scoring_version="organizer-relative-effect-novelty-v1",
            final_score=0.50 - rank / 100,
            lipid_score=0.38,
            tox_risk=0.25,
            novelty_score=0.70,
            lipid_rationale="药效团: carboxylic acid, aromatic ring",
            selection_tier="similarity_strict",
        )

    runtime = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    session = runtime.create_session()
    session.last_result = SimpleNamespace(
        run_id="mm-top4-top5",
        top_molecules=[molecule(rank) for rank in range(1, 6)],
        reserve_molecules=[],
        scored_molecules=[],
    )

    events = list(runtime.handle_message(session, text))
    assert "tool_start" not in [event.get("type") for event in events]
    reply = next(event["text"] for event in events if event.get("type") == "assistant")
    assert "| Top 4 | T4 |" in reply
    assert "| Top 5 | T5 |" in reply
    assert "Top 4 与 Top 5" in reply


def test_agent_introduces_ranked_molecule_without_export(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    from agent.memory import FileRunStore
    from agent.runtime.loop import AgentRuntime

    monkeypatch.setattr(
        "agent.runtime.loop.llm_plan_request",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("ranked-molecule introduction must bypass planning")
        ),
    )

    def molecule(rank: int) -> SimpleNamespace:
        return SimpleNamespace(
            molecule_id=f"T{rank}",
            selection_score=0.50 - rank / 100,
            competition_scoring_version="organizer-relative-effect-novelty-v1",
            final_score=0.50 - rank / 100,
            lipid_score=0.38,
            tox_risk=0.25,
            novelty_score=0.70,
            effect_rank=None,
            novelty_rank=None,
            lipid_rationale="药效团: carboxylic acid, aromatic ring",
            selection_tier="similarity_strict",
        )

    runtime = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    session = runtime.create_session()
    result = SimpleNamespace(
        run_id="mm-top5-introduction",
        top_molecules=[molecule(rank) for rank in range(1, 6)],
        reserve_molecules=[],
        scored_molecules=[],
    )
    session.last_result = result

    events = list(runtime.handle_message(session, "介绍一下排名top5的分子"))
    assert "tool_start" not in [event.get("type") for event in events]
    assert session.last_result is result
    assert session.top_n == 10
    reply = next(event["text"] for event in events if event.get("type") == "assistant")
    assert "T5 是上一轮冻结结果" in reply
    assert "Top 5" in reply
    assert "不会重新筛选" in reply


def test_handle_session_message_serializes_turns(monkeypatch, tmp_path) -> None:
    import threading

    from agent.memory import FileRunStore
    from agent.runtime.loop import AgentRuntime

    runtime = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    session = runtime.create_session()
    first_started = threading.Event()
    allow_first_to_finish = threading.Event()
    second_started = threading.Event()
    observed: list[str] = []

    def fake_handle(_session, text):
        observed.append(text)
        if text == "first":
            first_started.set()
            assert allow_first_to_finish.wait(timeout=2)
        else:
            second_started.set()
        yield {"type": "done"}

    monkeypatch.setattr(runtime, "handle_message", fake_handle)
    first = threading.Thread(
        target=lambda: list(runtime.handle_session_message(session.session_id, "first"))
    )
    second = threading.Thread(
        target=lambda: list(runtime.handle_session_message(session.session_id, "second"))
    )
    first.start()
    assert first_started.wait(timeout=1)
    second.start()
    assert not second_started.wait(timeout=0.15)
    allow_first_to_finish.set()
    assert second_started.wait(timeout=1)
    first.join(timeout=1)
    second.join(timeout=1)
    assert observed == ["first", "second"]


def test_agent_reruns_when_requested_top_n_exceeds_frozen_result(
    monkeypatch, tmp_path
) -> None:
    from types import SimpleNamespace

    from agent.memory import FileRunStore
    from agent.runtime.loop import AgentRuntime

    monkeypatch.setattr(
        "agent.runtime.loop.llm_json_decision",
        lambda **kwargs: ("execute_tools", "explicit generation"),
    )

    def molecule(index: int):
        return SimpleNamespace(
            molecule_id=f"T{index:05d}",
            selection_score=0.5 - index / 1000,
            final_score=0.5 - index / 1000,
            lipid_score=0.4,
            tox_risk=0.2,
            novelty_score=0.7,
            lipid_rationale="药效团: aromatic ring",
            selection_tier="similarity_strict",
        )

    config = SimpleNamespace(config_hash="config-hash")

    def result(count: int, run_id: str):
        molecules = [molecule(i) for i in range(1, count + 1)]
        return SimpleNamespace(
            run_id=run_id,
            output_count=count,
            selection_sha256=f"selection-{count}",
            reserve_selection_sha256="reserve",
            config=config,
            input_sha256="input",
            source_filename="library.sdf",
            top_molecules=molecules,
            reserve_molecules=[],
            scored_molecules=molecules,
            to_csv_text=lambda: "molecule_id\n" + "\n".join(m.molecule_id for m in molecules) + "\n",
            to_reserve_csv_text=lambda: "molecule_id\n",
        )

    captured: list[int] = []

    def fake_run(_path, *, top_n, **_kwargs):
        captured.append(top_n)
        return result(top_n, "mm-top15")

    monkeypatch.setattr("agent.runtime.loop.run_score_and_rank", fake_run)
    rt = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    session = rt.create_session()
    session.sdf_filename = "library.sdf"
    session.sdf_bytes = b"fake sdf"
    session.last_result = result(10, "mm-top10")

    events = list(rt.handle_message(session, "生成 Top15 候选清单 csv"))
    assert captured == [15]
    agent_plan = next(event for event in events if event.get("type") == "agent_plan")
    assert agent_plan["steps"] == [
        {"tool": "score_and_rank", "args": {"top_n": 15}},
        {"tool": "export_nomination", "args": {"tier": "primary"}},
    ]
    card = next(event["card"] for event in events if event.get("type") == "card")
    assert card["title"] == "候选分子清单：Top 15"
    assert card["filename"] == "library_nomination_top15.csv"
    assert len(session.last_result.top_molecules) == 15
    assert session.run_history[-1]["run_id"] == "mm-top15"
    assert session.run_history[-1]["top_n"] == 15


def test_pending_unexecutable_goal_blocks_silent_default_export(monkeypatch, tmp_path) -> None:
    from agent.memory import FileRunStore
    from agent.runtime.loop import AgentRuntime

    monkeypatch.setattr(
        "agent.runtime.loop.llm_json_decision",
        lambda **_kwargs: ("execute_tools", "explicit export"),
    )
    runtime = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    session = runtime.create_session()
    session.pending_goal = {
        "goal": "排除 PAINS 后筛选",
        "reason": "tool_contract_missing_parameters",
    }

    events = list(runtime.handle_message(session, "导出 CSV"))
    assert "tool_start" not in [event.get("type") for event in events]
    reply = next(event["text"] for event in events if event.get("type") == "assistant")
    assert "不会把这些条件静默替换成默认筛选" in reply


def test_default_masld_topn_request_executes_with_session_default_and_sdf(
    monkeypatch, tmp_path
) -> None:
    """Regression for an exported session where this request was blocked twice."""
    from types import SimpleNamespace

    from agent.memory import FileRunStore
    from agent.runtime.loop import AgentRuntime

    def unexpected_llm_plan(**_kwargs):
        raise AssertionError("direct deliverable request must not enter chat planning")

    monkeypatch.setattr("agent.runtime.loop.llm_plan_request", unexpected_llm_plan)

    def molecule(index: int):
        return SimpleNamespace(
            molecule_id=f"T{index:05d}",
            selection_score=0.6 - index / 1000,
            final_score=0.6 - index / 1000,
            lipid_score=0.4,
            tox_risk=0.2,
            novelty_score=0.7,
            lipid_rationale="药效团: aromatic ring",
            selection_tier="similarity_strict",
        )

    def fake_run(_path, *, top_n, **_kwargs):
        top = [molecule(index) for index in range(1, top_n + 1)]
        return SimpleNamespace(
            run_id="mm-default-top10",
            output_count=top_n,
            selection_sha256="selection-default-top10",
            reserve_selection_sha256="reserve-default-top10",
            config=SimpleNamespace(config_hash="config-default", reserve_n=20),
            input_sha256="input-default",
            source_filename="library.sdf",
            top_molecules=top,
            reserve_molecules=[],
            scored_molecules=top,
            to_csv_text=lambda: "molecule_id\n" + "\n".join(m.molecule_id for m in top) + "\n",
            to_reserve_csv_text=lambda: "molecule_id\n",
        )

    monkeypatch.setattr("agent.runtime.loop.run_score_and_rank", fake_run)
    runtime = AgentRuntime(store=FileRunStore(root=tmp_path / "runs"))
    session = runtime.create_session()
    session.sdf_filename = "library.sdf"
    session.sdf_bytes = b"fake sdf"
    # An earlier unsupported constraint may remain in the session, but this
    # turn explicitly elects the supported default path.
    session.pending_goal = {
        "goal": "按 PAINS 条件筛选",
        "reason": "tool_contract_missing_parameters",
    }

    events = list(runtime.handle_message(session, "使用当前默认 MASLD 筛选配置生成 TopN"))

    starts = [event["tool"] for event in events if event.get("type") == "tool_start"]
    assert starts == ["score_and_rank", "export_nomination"]
    assert session.pending_goal is None
    assert session.last_run_id == "mm-default-top10"
    assert len(session.artifacts) == 1
    plan = session.plan_history[-1]
    assert plan["status"] == "completed"
    assert [step["status"] for step in plan["steps"]] == ["succeeded", "succeeded"]


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
