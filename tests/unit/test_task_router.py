from agent.registry import AgentRegistry
from agent.runtime.task_router import TaskRouter


def test_router_uses_declared_capabilities_for_literature() -> None:
    router = TaskRouter(AgentRegistry())
    route = router.route_scp(
        "查找 MASLD 与 PPARα 激动剂的最新研究文献",
        enabled_skill_ids={"literature_research"},
    )
    assert route is not None
    assert route.capability_id == "literature_search"
    assert route.tool_id == "scp:Scholar-KG:query_paper"
    assert "MASLD" in route.arguments["query"]
    assert "Scientific exclusions:" in route.arguments["query"]
    assert "retina" in route.arguments["query"].lower()
    assert route.arguments["subject"] == "biology"


def test_router_selects_mechanism_relation_query() -> None:
    router = TaskRouter(AgentRegistry())
    route = router.route_scp(
        "在知识图谱中查找 PPARα 与脂肪酸氧化的关系",
        enabled_skill_ids={"mechanism_research"},
    )
    assert route is not None
    assert route.capability_id == "mechanism_relation_search"
    assert route.tool_id == "scp:SciGraph-Bio:query_cypher"
    assert "MATCH (n)-[r]-(m)" in route.arguments["cypher"]
    assert route.arguments["kg_name"] == "NAFLDkb"


def test_mechanism_capability_matches_role_question_without_domain_trigger() -> None:
    route = TaskRouter(AgentRegistry()).route_scp(
        "PPARα 在肝脏脂肪酸氧化中扮演什么角色？",
        enabled_skill_ids={"mechanism_research"},
    )
    assert route is not None
    assert route.capability_id == "mechanism_relation_search"


def test_multi_capability_routes_preserve_declared_evidence_order() -> None:
    router = TaskRouter(AgentRegistry())
    routes = router.route_scp_tasks(
        "查询 PPARα 在 MASLD 中的作用机制，查找相关文献，并设计包含对照组的实验方案。",
        enabled_skill_ids={
            "mechanism_research",
            "literature_research",
            "validation_protocol",
        },
    )
    assert [route.capability_id for route in routes] == [
        "mechanism_relation_search",
        "literature_search",
        "validation_protocol",
    ]
    assert router.evidence_dependencies("validation_protocol") == [
        "mechanism_relation_search",
        "literature_search",
    ]
    assert router.claim_scopes("validation_protocol") == [
        "experimental_design_advice"
    ]


def test_mechanism_recovery_is_plugin_declared_and_cross_skill() -> None:
    from agent.registry.models import ToolSpec

    registry = AgentRegistry()
    schemas = {
        "scp:SciGraph-Bio:get_node_labels": {
            "type": "object",
            "required": ["kg_name"],
            "properties": {"kg_name": {"type": "string"}},
        },
        "scp:SciGraph-Bio:get_relationship_types": {
            "type": "object",
            "required": ["kg_name"],
            "properties": {"kg_name": {"type": "string"}},
        },
        "scp:SciGraph-Bio:query_cypher": {
            "type": "object",
            "required": ["kg_name", "cypher"],
            "properties": {
                "kg_name": {"type": "string"},
                "cypher": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
        "scp:Scholar-KG:query_paper": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "subject": {"type": "string"},
                "top_k": {"type": "integer"},
            },
        },
    }
    for tool_id, schema in schemas.items():
        registry.register_dynamic_tool(
            ToolSpec(
                tool_id=tool_id,
                plugin_id="scp-hub",
                title=tool_id,
                input_schema=schema,
                dynamic=True,
            )
        )
    steps, fallback = TaskRouter(registry).recovery_steps(
        "mechanism_relation_search",
        "PPARα 在 MASLD 肝脏脂质代谢中扮演什么角色？",
        enabled_skill_ids={"mechanism_research", "literature_research"},
    )
    assert [step["evidence_role"] for step in steps] == [
        "schema_probe",
        "schema_probe",
        "evidence_query",
        "evidence_query",
    ]
    assert "MATCH (target)-[r]-(neighbor)" in steps[2]["arguments"]["cypher"]
    assert "[*1..2]" in steps[3]["arguments"]["cypher"]
    assert "lipid metabolism" in steps[3]["arguments"]["cypher"]
    assert fallback is not None
    assert fallback.capability_id == "literature_search"
    assert fallback.tool_id == "scp:Scholar-KG:query_paper"
    assert "(MASLD OR NAFLD OR NASH)" in fallback.arguments["query"]
    assert '(liver OR hepatic)' in fallback.arguments["query"]
    assert '"lipid metabolism"' in fallback.arguments["query"]
    assert " NOT (" in fallback.arguments["query"]


def test_router_selects_validation_protocol() -> None:
    route = TaskRouter(AgentRegistry()).route_scp(
        "请给出包含对照组和剂量梯度的验证方案",
        enabled_skill_ids={"validation_protocol"},
    )
    assert route is not None
    assert route.capability_id == "validation_protocol"
    assert route.tool_id == "scp:Thoth-Plan:protocol_generation"
    assert route.arguments["user_prompt"].startswith("请给出")


def test_router_does_not_route_disabled_skill() -> None:
    router = TaskRouter(AgentRegistry())
    assert router.route_scp("查询 MASLD 最新文献", enabled_skill_ids=set()) is None


def test_literature_query_keeps_exclusions_out_of_positive_anchors() -> None:
    route = TaskRouter(AgentRegistry()).route_scp(
        "查找 MASLD 研究，排除酒精性肝病和视网膜相关论文",
        enabled_skill_ids={"literature_research"},
    )
    assert route is not None
    assert "Scientific exclusions:" in route.arguments["query"]
    positive = route.arguments["query"].split("Scientific exclusions:", 1)[0]
    assert "alcoholic liver disease" not in positive.lower()
    assert "retinal" not in positive.lower()


def test_non_liver_exclusion_does_not_exclude_liver() -> None:
    route = TaskRouter(AgentRegistry()).route_scp(
        "查找 MASLD 肝脏研究，排除神经系统和非肝脏组织研究",
        enabled_skill_ids={"literature_research"},
    )
    assert route is not None
    exclusions = route.arguments["query"].split("Scientific exclusions:", 1)[1]
    excluded_terms = {value.strip().lower() for value in exclusions.split(";")}
    assert "肝脏" not in excluded_terms
    assert "liver" not in excluded_terms
    assert "hepatic" not in excluded_terms


def test_planner_falls_back_to_registry_route_when_llm_is_not_ready(monkeypatch) -> None:
    from types import SimpleNamespace
    from plugins.molmind_core.scientific.mechanism import llm_client

    monkeypatch.setattr(
        llm_client,
        "resolve_llm_settings",
        lambda *args, **kwargs: SimpleNamespace(ready=False),
    )
    router = TaskRouter(AgentRegistry())
    route = router.plan_scp(
        "查询 MASLD 最新文献",
        enabled_skill_ids={"literature_research"},
    )
    assert route is not None
    assert route.planner_status == "deterministic"
    assert route.tool_id == "scp:Scholar-KG:query_paper"


def test_argument_schema_rejects_missing_and_unknown_fields() -> None:
    schema = {
        "type": "object",
        "required": ["query"],
        "additionalProperties": False,
        "properties": {"query": {"type": "string"}},
    }
    assert TaskRouter._arguments_valid({"query": "MASLD"}, schema)
    assert not TaskRouter._arguments_valid({}, schema)
    assert not TaskRouter._arguments_valid({"query": "MASLD", "unsafe": True}, schema)


def test_planner_cannot_override_plugin_owned_defaults(monkeypatch) -> None:
    from types import SimpleNamespace
    from agent.registry.models import ToolSpec
    from plugins.molmind_core.scientific.mechanism import llm_client

    registry = AgentRegistry()
    registry.register_dynamic_tool(
        ToolSpec(
            tool_id="scp:Scholar-KG:query_paper",
            plugin_id="scp-hub",
            title="query paper",
            input_schema={
                "type": "object",
                "required": ["query"],
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string"},
                    "subject": {"type": "string"},
                    "top_k": {"type": "integer"},
                },
            },
        )
    )
    monkeypatch.setattr(
        llm_client,
        "resolve_llm_settings",
        lambda *args, **kwargs: SimpleNamespace(
            ready=True, enabled=True, model="test", base_url="", api_key="",
            temperature=0, timeout_sec=5, max_tokens=400, cache_dir="", use_cache=False,
        ),
    )
    monkeypatch.setattr(
        llm_client,
        "chat_completion",
        lambda *args, **kwargs: '{"route":"scp","capability_id":"literature_search",'
        '"skill_id":"literature_research","tool_id":"scp:Scholar-KG:query_paper",'
        '"arguments":{"query":"MASLD PPARA","subject":"MASLD","top_k":99},'
        '"confidence":0.9,"reason":"semantic match"}',
    )
    route = TaskRouter(registry).plan_scp(
        "找关于肝脏脂肪酸氧化的论文",
        enabled_skill_ids={"literature_research"},
    )
    assert route is not None
    assert route.planner_status == "llm"
    assert route.arguments["subject"] == "biology"
    assert route.arguments["top_k"] == 5
    assert "找关于肝脏脂肪酸氧化的论文" in route.arguments["query"]
    assert "fatty acid oxidation" in route.arguments["query"]
    assert "MASLD PPARA" not in route.arguments["query"]
    assert route.arguments["query"].count("Scientific domain constraints:") == 1


def test_semantic_planner_can_route_without_task_term_match(monkeypatch) -> None:
    from types import SimpleNamespace
    from agent.registry.models import ToolSpec
    from plugins.molmind_core.scientific.mechanism import llm_client

    registry = AgentRegistry()
    registry.register_dynamic_tool(
        ToolSpec(
            tool_id="scp:Scholar-KG:query_paper",
            plugin_id="scp-hub",
            title="query paper",
            input_schema={"type": "object", "properties": {}},
        )
    )
    monkeypatch.setattr(
        llm_client,
        "resolve_llm_settings",
        lambda *args, **kwargs: SimpleNamespace(
            ready=True, enabled=True, model="test", base_url="", api_key="",
            temperature=0, timeout_sec=5, max_tokens=400, cache_dir="", use_cache=False,
        ),
    )
    monkeypatch.setattr(
        llm_client,
        "chat_completion",
        lambda *args, **kwargs: '{"route":"scp","capability_id":"literature_search",'
        '"skill_id":"literature_research","tool_id":"scp:Scholar-KG:query_paper",'
        '"arguments":{"query":"hepatic beta oxidation advances"},'
        '"confidence":0.88,"reason":"semantic capability match"}',
    )
    route = TaskRouter(registry).plan_scp(
        "我想了解肝脏代谢领域近期有哪些值得关注的进展",
        enabled_skill_ids={"literature_research"},
    )
    assert route is not None
    assert route.capability_id == "literature_search"
    assert route.planner_status == "llm"


def test_planner_returns_chat_for_unrelated_conversation(monkeypatch) -> None:
    from types import SimpleNamespace
    from plugins.molmind_core.scientific.mechanism import llm_client

    monkeypatch.setattr(
        llm_client,
        "resolve_llm_settings",
        lambda *args, **kwargs: SimpleNamespace(
            ready=True, enabled=True, model="test", base_url="", api_key="",
            temperature=0, timeout_sec=5, max_tokens=400, cache_dir="", use_cache=False,
        ),
    )
    monkeypatch.setattr(
        llm_client,
        "chat_completion",
        lambda *args, **kwargs: '{"route":"chat","capability_id":"",'
        '"skill_id":"","tool_id":"","arguments":{},'
        '"confidence":0.99,"reason":"not a plugin task"}',
    )
    route = TaskRouter(AgentRegistry()).plan_scp(
        "你好，请介绍一下你自己",
        enabled_skill_ids={"literature_research"},
    )
    assert route is not None
    assert route.route == "chat"


def test_preflight_identifies_uninstalled_semantic_capability(monkeypatch) -> None:
    from types import SimpleNamespace
    from plugins.molmind_core.scientific.mechanism import llm_client

    monkeypatch.setattr(
        llm_client,
        "resolve_llm_settings",
        lambda *args, **kwargs: SimpleNamespace(
            ready=True, enabled=True, model="test", base_url="", api_key="",
            temperature=0, timeout_sec=5, max_tokens=400, cache_dir="", use_cache=False,
        ),
    )
    monkeypatch.setattr(
        llm_client,
        "chat_completion",
        lambda *args, **kwargs: '{"route":"scp","capability_id":"mechanism_relation_search",'
        '"skill_id":"mechanism_research","tool_id":"scp:SciGraph-Bio:query_cypher",'
        '"arguments":{},"confidence":0.91,"reason":"semantic mechanism request"}',
    )
    route = TaskRouter(AgentRegistry()).plan_scp(
        "PPARα 在肝脏脂肪酸氧化中扮演什么角色？",
        enabled_skill_ids={"mechanism_research"},
        allow_unregistered=True,
    )
    assert route is not None
    assert route.skill_id == "mechanism_research"
    assert route.planner_status == "llm_preflight"


def test_planner_honors_llm_choice_among_enabled_capabilities(monkeypatch) -> None:
    """task_terms hint must not narrow candidates; valid LLM choice wins."""
    from types import SimpleNamespace
    from agent.registry.models import ToolSpec
    from plugins.molmind_core.scientific.mechanism import llm_client

    registry = AgentRegistry()
    for tool_id, title in (
        ("scp:SciGraph-Bio:query_cypher", "cypher"),
        ("scp:Scholar-KG:query_paper", "paper"),
    ):
        registry.register_dynamic_tool(
            ToolSpec(
                tool_id=tool_id,
                plugin_id="scp-hub",
                title=title,
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "subject": {"type": "string"},
                        "top_k": {"type": "integer"},
                        "kg_name": {"type": "string"},
                        "cypher": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                },
            )
        )
    monkeypatch.setattr(
        llm_client,
        "resolve_llm_settings",
        lambda *args, **kwargs: SimpleNamespace(
            ready=True, enabled=True, model="test", base_url="", api_key="",
            temperature=0, timeout_sec=5, max_tokens=400, cache_dir="", use_cache=False,
        ),
    )
    monkeypatch.setattr(
        llm_client,
        "chat_completion",
        lambda *args, **kwargs: '{"route":"scp","capability_id":"literature_search",'
        '"skill_id":"literature_research","tool_id":"scp:Scholar-KG:query_paper",'
        '"arguments":{"query":"PPAR MASLD literature"},"confidence":0.99,'
        '"reason":"user wants papers"}',
    )
    route = TaskRouter(registry).plan_scp(
        "PPARα 在 MASLD 肝脏脂质代谢中扮演什么角色？",
        enabled_skill_ids={"mechanism_research", "literature_research"},
    )
    assert route is not None
    assert route.capability_id == "literature_search"
    assert route.planner_status == "llm"


def test_planner_falls_back_when_llm_picks_unregistered_tool(monkeypatch) -> None:
    from types import SimpleNamespace
    from agent.registry.models import ToolSpec
    from plugins.molmind_core.scientific.mechanism import llm_client

    registry = AgentRegistry()
    registry.register_dynamic_tool(
        ToolSpec(
            tool_id="scp:SciGraph-Bio:query_cypher",
            plugin_id="scp-hub",
            title="cypher",
            input_schema={"type": "object", "properties": {}},
        )
    )
    monkeypatch.setattr(
        llm_client,
        "resolve_llm_settings",
        lambda *args, **kwargs: SimpleNamespace(
            ready=True, enabled=True, model="test", base_url="", api_key="",
            temperature=0, timeout_sec=5, max_tokens=400, cache_dir="", use_cache=False,
        ),
    )
    monkeypatch.setattr(
        llm_client,
        "chat_completion",
        lambda *args, **kwargs: '{"route":"scp","capability_id":"literature_search",'
        '"skill_id":"literature_research","tool_id":"scp:Scholar-KG:query_paper",'
        '"arguments":{"query":"wrong route"},"confidence":0.99,"reason":"override"}',
    )
    route = TaskRouter(registry).plan_scp(
        "PPARα 在 MASLD 肝脏脂质代谢中扮演什么角色？",
        enabled_skill_ids={"mechanism_research", "literature_research"},
    )
    assert route is not None
    assert route.capability_id == "mechanism_relation_search"
    assert route.planner_status == "deterministic"


def test_missing_literature_skill_clarifies_install_instead_of_chat(monkeypatch) -> None:
    from types import SimpleNamespace

    from agent.intent import parse_intent
    from plugins.molmind_core.scientific.mechanism import llm_client

    # LLM-down path: task_terms may propose install only when session-act is unavailable.
    monkeypatch.setattr(
        llm_client,
        "resolve_llm_settings",
        lambda *args, **kwargs: SimpleNamespace(
            ready=False, enabled=False, model="", base_url="", api_key="",
            temperature=0, timeout_sec=5, max_tokens=400, cache_dir="", use_cache=False,
        ),
    )

    router = TaskRouter(AgentRegistry())
    session = SimpleNamespace(
        last_result=None,
        frozen_ranking=None,
        last_run_id="",
        run_history=[],
        installed_scp_skills={"mechanism_research": {"enabled": True}},
        turn_execution_gate=None,
        messages=[],
    )
    for text in (
        "查询 MASLD 最新文献，允许联网",
        "检索 PPAR 与脂肪肝相关论文，允许使用实时资料",
    ):
        route = router.route(parse_intent(text), session)
        assert route.route == "clarify"
        assert route.skill_id == "literature_research"
        assert route.reason == "scp_skill_not_installed:literature_research"
        assert route.planner_status == "deterministic_offline"


def test_session_act_propose_install_beats_task_terms(monkeypatch) -> None:
    """Live session act must win over any task_terms install short-circuit."""
    from types import SimpleNamespace

    from agent.intent import parse_intent
    from plugins.molmind_core.scientific.mechanism import llm_client

    monkeypatch.setattr(
        llm_client,
        "resolve_llm_settings",
        lambda *args, **kwargs: SimpleNamespace(
            ready=True, enabled=True, model="test", base_url="", api_key="",
            temperature=0, timeout_sec=5, max_tokens=400, cache_dir="", use_cache=False,
        ),
    )

    calls: list[str] = []

    def _chat(*args, **kwargs):
        system = str(kwargs.get("system") or "")
        if "会话决策器" in system:
            calls.append("session_act")
            return (
                '{"act":"propose_install","skill_id":"literature_research",'
                '"confidence":0.95,"reason":"needs literature skill"}'
            )
        calls.append("plan_scp")
        return (
            '{"route":"chat","capability_id":"","skill_id":"","tool_id":"",'
            '"arguments":{},"confidence":0.5,"reason":"should not reach"}'
        )

    monkeypatch.setattr(llm_client, "chat_completion", _chat)

    router = TaskRouter(AgentRegistry())
    session = SimpleNamespace(
        last_result=None,
        frozen_ranking=None,
        last_run_id="",
        run_history=[],
        installed_scp_skills={"mechanism_research": {"enabled": True}},
        turn_execution_gate=None,
        messages=[],
    )
    route = router.route(parse_intent("查询 MASLD 最新文献，允许联网"), session)
    assert route.route == "clarify"
    assert route.skill_id == "literature_research"
    assert route.reason == "scp_skill_not_installed:literature_research"
    assert route.planner_status == "llm"
    assert calls == ["session_act"]


def test_missing_scp_skill_emits_install_request_card(monkeypatch) -> None:
    from types import SimpleNamespace

    from agent.memory.models import AgentSession
    from agent.runtime.loop import AgentRuntime
    from plugins.molmind_core.scientific.mechanism import llm_client
    from tests.unit.agent_test_support import MemRunStore

    monkeypatch.setattr(
        llm_client,
        "resolve_llm_settings",
        lambda *args, **kwargs: SimpleNamespace(
            ready=False, enabled=False, model="", base_url="", api_key="",
            temperature=0, timeout_sec=5, max_tokens=400, cache_dir="", use_cache=False,
        ),
    )

    runtime = AgentRuntime(store=MemRunStore())
    session = AgentSession(session_id="scp_install_card_0001", client_id="scp_install_card_0001")
    session.installed_scp_skills = {
        "mechanism_research": {"skill_id": "mechanism_research", "enabled": True, "tools": []}
    }
    events = list(
        runtime.handle_message(session, "查询 MASLD 最新文献，允许联网")
    )
    types = [event.get("type") for event in events]
    assert "install_request" in types
    assert types[-2:] == ["assistant", "done"]
    req = next(event for event in events if event.get("type") == "install_request")
    assert req.get("kind") == "scp_skill"
    assert req.get("skill_id") == "literature_research"
    assert req.get("retry_text") == "查询 MASLD 最新文献，允许联网"
    assert any(
        isinstance(item, dict) and item.get("skill_id") == "literature_research"
        for item in (req.get("skills") or [])
    )
    assistant = next(event for event in events if event.get("type") == "assistant")
    assert "工具与插件" not in str(assistant.get("text") or "")
    assert "安装" in str(assistant.get("text") or "")
    assert "自动重试" not in str(assistant.get("text") or "")
