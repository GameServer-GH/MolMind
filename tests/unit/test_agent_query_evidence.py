"""Agent query_evidence: explicit R0 invocation, audit states and live opt-in."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from agent.intent import parse_intent
from agent.memory import FileRunStore
from agent.runtime.loop import AgentRuntime


@dataclass
class _QueryResult:
    ok: bool
    error_code: str = ""
    message: str = ""
    card: dict[str, Any] = field(default_factory=dict)
    bundle: Any = None
    degraded_channels: list[str] = field(default_factory=list)
    identity: dict[str, Any] = field(default_factory=dict)


def _runtime(tmp_path) -> tuple[AgentRuntime, Any]:
    runtime = AgentRuntime(store=FileRunStore(root=tmp_path / "agent_runs"))
    session = runtime.create_session()
    session.last_result = SimpleNamespace(scored_molecules=[])
    session.last_run_id = "run-1"
    session.last_selection_sha256 = "selection-stable"
    session.last_molecule_index = {
        "T001": [
            {
                "molecule_id": "T001",
                "inchikey": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
                "cas": "64-17-5",
                "smiles": "CCO",
                "original_smiles": "OCC",
                "standardization_steps": ["canonical_smiles"],
            }
        ]
    }
    return runtime, session


def _stub_success(calls: list[dict[str, Any]]):
    def run_query_evidence(**kwargs):
        calls.append(kwargs)
        sink = kwargs["event_sink"]
        sink(
            {
                "type": "local_hit",
                "provider": "snapshot",
                "status": "hit",
                "count": 1,
            }
        )
        return _QueryResult(
            ok=True,
            message="已找到 1 条本地证据；该证据不修改主榜。",
            card={
                "status": "hit",
                "provider_statuses": {"snapshot": {"status": "hit"}},
                "claim_ceiling": "proxy_priority_only",
            },
            identity={
                "molecule_id": kwargs.get("molecule_id"),
                "lookup_field": "standardized_inchikey",
                "lookup_value": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
                "match_type": "exact_identity",
            },
        )

    return run_query_evidence


def test_natural_language_evidence_intent_precedes_nomination() -> None:
    intent = parse_intent(
        "帮我查询候选 T001 的 ChEMBL 毒性证据 "
        "providers=chembl,pubchem query_types=tox,annotation allow_live=true force_refresh=true"
    )
    assert intent.query_evidence is True
    assert intent.want_csv is False
    assert intent.skill_ids == ("masld_explain",)
    assert intent.evidence_molecule_id == "T001"
    assert intent.evidence_providers == ("chembl", "pubchem")
    assert intent.evidence_query_types == ("tox", "annotation")
    assert intent.evidence_allow_live is True
    assert intent.evidence_force_refresh is True


def test_scientific_evidence_qualifier_does_not_trigger_core_evidence() -> None:
    intent = parse_intent("请总结 MASLD 与 PPARα 激动剂的证据和最新研究")
    assert intent.query_evidence is False
    assert intent.wants_tools is False


def test_live_requires_explicit_opt_in() -> None:
    offline = parse_intent("查询候选 T001 的证据，看看联网来源")
    enabled = parse_intent("查询候选 T001 的证据，开启联网补证")
    negated = parse_intent("查询候选 T001 的证据，但不要开启联网查询")
    explicit_false = parse_intent(
        "查询候选 T001 的证据，allow_live=false；不要开启联网补证"
    )
    assert offline.query_evidence is True
    assert offline.evidence_allow_live is False
    assert enabled.evidence_allow_live is True
    assert negated.evidence_allow_live is False
    assert explicit_false.evidence_allow_live is False


def test_evidence_intent_extracts_direct_identity_fields() -> None:
    intent = parse_intent(
        "查询证据 InChIKey=LFQSCWFLJHTTHZ-UHFFFAOYSA-N "
        "CAS=64-17-5 SMILES='CCO'"
    )
    assert intent.query_evidence is True
    assert intent.evidence_inchikey == "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
    assert intent.evidence_cas == "64-17-5"
    assert intent.evidence_smiles == "CCO"


def test_query_evidence_tool_success_defaults_offline_and_preserves_selection(
    monkeypatch, tmp_path
) -> None:
    import plugins.molmind_core.tools.scientific as scientific_tools

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        scientific_tools,
        "run_query_evidence",
        _stub_success(calls),
        raising=False,
    )
    runtime, session = _runtime(tmp_path)

    events = list(runtime.handle_message(session, "/tool:query_evidence T001"))

    assert len(calls) == 1
    assert calls[0]["molecule_id"] == "T001"
    assert calls[0]["molecule_index"] == session.last_molecule_index
    assert calls[0]["allow_live"] is False
    assert calls[0]["force_refresh"] is False
    assert not any(e.get("tool") == "score_and_rank" for e in events)
    start = next(e for e in events if e.get("type") == "tool_start")
    assert start["tool"] == "query_evidence"
    assert start["args"]["allow_live"] is False
    assert start["args"]["writes_selection"] is False
    assert any(e.get("type") == "local_hit" for e in events)
    card = next(e["card"] for e in events if e.get("type") == "card")
    assert card["kind"] == "evidence"
    assert card["allow_live"] is False
    end = next(e for e in events if e.get("type") == "tool_end")
    assert end["digest"]["selection_sha256_unchanged"] is True
    assert end["digest"]["writes_selection"] is False
    assert session.last_selection_sha256 == "selection-stable"


def test_default_provider_plan_is_visible_before_execution(monkeypatch, tmp_path) -> None:
    import plugins.molmind_core.tools.scientific as scientific_tools

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        scientific_tools,
        "run_query_evidence",
        _stub_success(calls),
        raising=False,
    )
    runtime, session = _runtime(tmp_path)
    events = list(runtime.handle_message(session, "/tool:query_evidence T001"))

    plan = next(event for event in events if event.get("type") == "query_plan")
    start = next(event for event in events if event.get("type") == "tool_start")
    assert plan["providers"] == ["epa_ctx", "chembl", "pubchem"]
    assert start["args"]["providers"] == plan["providers"]
    assert start["args"]["provider_selection"] == "default"
    # Displaying defaults must not turn them into an explicit filter passed to
    # the canonical handler; local/QC evidence stays visible.
    assert calls[0]["providers"] is None


def test_agent_to_real_tool_to_local_facade_end_to_end(monkeypatch, tmp_path) -> None:
    """Exercise the real handler; only inject isolated local paths, not a stub."""
    import plugins.molmind_core.tools.scientific as scientific_tools

    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "fixture.jsonl").write_text(
        json.dumps(
            {
                "inchikey": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
                "cas": "64-17-5",
                "adapter_id": "chembl_lipid_v1",
                "query_type": "lipid",
                "score": 0.71,
                "confidence": 0.62,
                "evidence_id": "chembl:e2e:lipid",
                "endpoint": "cellular_lipid_reduction",
                "direction": "supports",
                "evidence_role": "task_evidence",
                "query_status": "exact_hit",
                "schema_version": "evidence-v2",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    provider_config = tmp_path / "providers.yaml"
    provider_config.write_text(
        """
schema_version: test-v1
cache:
  state_db: ignored.sqlite
providers:
  chembl:
    enabled: true
    query_tool_default: true
    live_supported: true
    identity_order: [original_inchikey, standardized_inchikey]
    endpoint: molecule_activity
    query_types:
      lipid: {endpoint: molecule_activity}
    adapter_version: chembl-test-v1
""".lstrip(),
        encoding="utf-8",
    )
    canonical = scientific_tools.run_query_evidence

    def isolated_real_handler(**kwargs):
        return canonical(
            **kwargs,
            snapshot_dir=snapshot,
            provider_config_path=provider_config,
            cache_path=tmp_path / "query-state.sqlite",
        )

    monkeypatch.setattr(
        scientific_tools,
        "run_query_evidence",
        isolated_real_handler,
    )
    runtime, session = _runtime(tmp_path)

    events = list(runtime.handle_message(session, "/tool:query_evidence T001"))

    assert not any(event.get("tool") == "score_and_rank" for event in events)
    card = next(event["card"] for event in events if event.get("type") == "card")
    assert card["status"] == "hit"
    assert card["provider_statuses"]["chembl"]["status"] == "hit"
    assert card["evidence_items"][0]["evidence_id"] == "chembl:e2e:lipid"
    assert card["evidence_items"][0]["participates_in_ranking"] is False
    assert session.last_selection_sha256 == "selection-stable"


def test_query_evidence_missing_input_returns_audit_missing(monkeypatch, tmp_path) -> None:
    import plugins.molmind_core.tools.scientific as scientific_tools

    def missing(**kwargs):
        assert kwargs["molecule_id"] is None
        return _QueryResult(
            ok=False,
            error_code="audit_missing",
            message="缺少可解析身份；请提供 molecule_id、InChIKey、CAS 或 SMILES。",
            card={"status": "audit_missing"},
        )

    monkeypatch.setattr(scientific_tools, "run_query_evidence", missing, raising=False)
    runtime = AgentRuntime(store=FileRunStore(root=tmp_path / "agent_runs"))
    session = runtime.create_session()
    events = list(runtime.handle_message(session, "/tool:query_evidence"))

    assert any(e.get("type") == "tool_start" for e in events)
    card = next(e["card"] for e in events if e.get("type") == "card")
    assert card["status"] == "audit_missing"
    text = " ".join(e.get("text", "") for e in events if e.get("type") == "assistant")
    assert "缺少可解析身份" in text
    assert "当前该项不支持独立" not in text


def test_query_evidence_unknown_molecule_id_is_not_guessed(monkeypatch, tmp_path) -> None:
    import plugins.molmind_core.tools.scientific as scientific_tools

    calls: list[dict[str, Any]] = []

    def unknown(**kwargs):
        calls.append(kwargs)
        return _QueryResult(
            ok=False,
            error_code="unknown_molecule_id",
            message="当前 Run 中未找到 molecule_id=T404；未猜测其他候选。",
            card={"status": "audit_missing"},
            identity={"molecule_id": "T404", "match_type": "unresolved"},
        )

    monkeypatch.setattr(scientific_tools, "run_query_evidence", unknown, raising=False)
    runtime, session = _runtime(tmp_path)
    events = list(runtime.handle_message(session, "/tool:query_evidence T404"))

    assert calls[0]["molecule_id"] == "T404"
    assert calls[0]["inchikey"] is None
    assert calls[0]["cas"] is None
    assert calls[0]["smiles"] is None
    text = " ".join(e.get("text", "") for e in events if e.get("type") == "assistant")
    assert "未找到" in text
    assert "未猜测" in text


def test_query_evidence_explicit_live_is_visible_in_events(monkeypatch, tmp_path) -> None:
    import plugins.molmind_core.tools.scientific as scientific_tools

    calls: list[dict[str, Any]] = []

    def live(**kwargs):
        calls.append(kwargs)
        kwargs["event_sink"]({"type": "remote_start", "provider": "chembl"})
        kwargs["event_sink"](
            {
                "type": "remote_end",
                "provider": "chembl",
                "status": "verified_empty",
            }
        )
        return _QueryResult(
            ok=True,
            message="ChEMBL 查询完成，未发现记录；这不是生物学阴性。",
            card={
                "status": "verified_empty",
                "provider_statuses": {"chembl": {"status": "verified_empty"}},
            },
            identity={"molecule_id": "T001", "match_type": "exact_identity"},
        )

    monkeypatch.setattr(scientific_tools, "run_query_evidence", live, raising=False)
    runtime, session = _runtime(tmp_path)
    events = list(
        runtime.handle_message(
            session,
            "/tool:query_evidence T001 allow_live=true force_refresh=true providers=chembl",
        )
    )

    assert calls[0]["allow_live"] is True
    assert calls[0]["force_refresh"] is True
    start = next(e for e in events if e.get("type") == "tool_start")
    assert start["args"]["allow_live"] is True
    assert [e["type"] for e in events if e.get("type", "").startswith("remote_")] == [
        "remote_start",
        "remote_end",
    ]
    card = next(e["card"] for e in events if e.get("type") == "card")
    assert card["allow_live"] is True


def test_provider_events_stream_before_handler_finishes(monkeypatch, tmp_path) -> None:
    import plugins.molmind_core.tools.scientific as scientific_tools

    release = threading.Event()

    def streaming(**kwargs):
        kwargs["event_sink"]({"type": "remote_start", "provider": "chembl"})
        assert release.wait(timeout=2.0)
        kwargs["event_sink"](
            {"type": "remote_end", "provider": "chembl", "status": "hit"}
        )
        return _QueryResult(ok=True, message="完成", card={"status": "hit"})

    monkeypatch.setattr(scientific_tools, "run_query_evidence", streaming, raising=False)
    runtime, session = _runtime(tmp_path)
    event_iter = runtime.handle_message(
        session,
        "/tool:query_evidence T001 allow_live=true providers=chembl",
    )

    seen: list[dict[str, Any]] = []
    while True:
        event = next(event_iter)
        seen.append(event)
        if event.get("type") == "remote_start":
            break
    assert not release.is_set()
    assert [e["type"] for e in seen if e.get("type") in {"query_plan", "remote_start"}] == [
        "query_plan",
        "remote_start",
    ]
    release.set()
    remaining = list(event_iter)
    ordered = [
        e["type"]
        for e in [*seen, *remaining]
        if e.get("type") in {"remote_start", "remote_end", "tool_end", "card"}
    ]
    assert ordered == ["remote_start", "remote_end", "tool_end", "card"]


def test_read_only_tool_mutation_is_blocked_and_success_summary_corrected(
    monkeypatch, tmp_path
) -> None:
    import plugins.molmind_core.tools.scientific as scientific_tools

    def mutating(**kwargs):
        kwargs["event_sink"](
            {"type": "query_summary", "status": "hit", "message": "success"}
        )
        kwargs["result"].selection_sha256 = "illicit-change"
        return _QueryResult(
            ok=True,
            message="success",
            card={"status": "hit", "summary": "success"},
        )

    monkeypatch.setattr(
        scientific_tools,
        "run_query_evidence",
        mutating,
        raising=False,
    )
    runtime, session = _runtime(tmp_path)
    events = list(runtime.handle_message(session, "/tool:query_evidence T001"))

    summaries = [
        event for event in events if event.get("type") == "query_summary"
    ]
    assert [event["status"] for event in summaries] == [
        "hit",
        "selection_mutation_blocked",
    ]
    end = next(event for event in events if event.get("type") == "tool_end")
    assert end["ok"] is False
    assert end["error"] == "selection_mutation_blocked"
    card = next(event["card"] for event in events if event.get("type") == "card")
    assert card["status"] == "selection_mutation_blocked"
    assert "已阻断" in card["summary"]
    assert session.last_selection_sha256 == "selection-stable"
    assert session.last_result.selection_sha256 == ""


def test_selection_guard_digest_ignores_opaque_object_memory_addresses() -> None:
    from agent.runtime.loop import _selection_guard_snapshot

    class _Opaque:
        pass

    @dataclass
    class _Record:
        molecule_id: str
        cached_object: Any

    result = SimpleNamespace(
        top_molecules=[_Record("T001", _Opaque())],
        reserve_molecules=[],
        scored_molecules=[_Record("T001", _Opaque())],
        selection_sha256="selection-stable",
    )

    snapshot1, digest1 = _selection_guard_snapshot(result)
    _snapshot2, digest2 = _selection_guard_snapshot(result)

    assert digest1 == digest2
    assert snapshot1["top_molecules"][0] is result.top_molecules[0]


def test_masld_explain_mention_invokes_query_without_llm(monkeypatch, tmp_path) -> None:
    import plugins.molmind_core.tools.scientific as scientific_tools

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        scientific_tools,
        "run_query_evidence",
        _stub_success(calls),
        raising=False,
    )
    runtime, session = _runtime(tmp_path)
    events = list(runtime.handle_message(session, "/skill:masld_explain T001"))
    assert calls and calls[0]["molecule_id"] == "T001"
    assert any(e.get("tool") == "query_evidence" for e in events)


def test_molecule_identity_index_round_trips_and_detach_clears(tmp_path) -> None:
    root = tmp_path / "agent_runs"
    runtime, session = _runtime(tmp_path)
    runtime.store.persist(session)

    loaded = FileRunStore(root=root).get(session.session_id)
    assert loaded is not None
    assert loaded.last_molecule_index["T001"][0]["original_smiles"] == "OCC"
    assert loaded.last_molecule_index["T001"][0]["standardization_steps"] == [
        "canonical_smiles"
    ]

    runtime.detach_sdf(session)
    assert session.last_molecule_index == {}


def test_agent_api_stream_executes_query_evidence_tool(monkeypatch, tmp_path) -> None:
    from fastapi.testclient import TestClient

    from agent.runtime import loop as loop_mod
    from apps.api.app import app
    import plugins.molmind_core.tools.scientific as scientific_tools

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        scientific_tools,
        "run_query_evidence",
        _stub_success(calls),
        raising=False,
    )
    runtime = AgentRuntime(store=FileRunStore(root=tmp_path / "api_agent_runs"))
    monkeypatch.setattr(loop_mod, "_RUNTIME", runtime)
    client = TestClient(app)
    client.headers.update({"X-MolMind-Client-ID": "browser_test_query_0001"})
    session_id = client.post("/api/agent/sessions").json()["session_id"]
    session = runtime.get_session(session_id)
    assert session is not None
    session.last_selection_sha256 = "selection-stable"
    session.last_molecule_index = {
        "T001": [
            {
                "molecule_id": "T001",
                "inchikey": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
                "cas": None,
                "smiles": "CCO",
            }
        ]
    }

    with client.stream(
        "POST",
        f"/api/agent/sessions/{session_id}/message/stream",
        json={"text": "/tool:query_evidence T001"},
    ) as response:
        assert response.status_code == 200
        events = [json.loads(line) for line in response.iter_lines() if line]

    assert calls and calls[0]["molecule_id"] == "T001"
    assert [
        event["type"]
        for event in events
        if event.get("type") in {"query_plan", "tool_start", "local_hit", "tool_end", "card", "done"}
    ] == ["query_plan", "tool_start", "local_hit", "tool_end", "card", "done"]
