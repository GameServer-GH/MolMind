from __future__ import annotations

import json
import time

import httpx
import pytest

# Import agent registry before scp_hub.registry to avoid circular import via agent.__init__.
from agent.registry import AgentRegistry
from agent.registry.models import ToolSpec
from plugins.scp_hub.cache import SCPQueryCache
from plugins.scp_hub.client import MCPClient, MCPError
from plugins.scp_hub.credentials import get_api_key
from plugins.scp_hub.jobs import SCPJobManager
from plugins.scp_hub.models import SCPObservation
from plugins.scp_hub.observations import observation_to_evidence_hit
from plugins.scp_hub.policy import validate_endpoint, validate_outbound_arguments
from plugins.scp_hub.registry import SCPRegistryManager
from plugins.scp_hub.skill_importer import import_skill_markdown


def _transport(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    method = body["method"]
    if method == "initialize":
        return httpx.Response(200, headers={"Mcp-Session-Id": "s1"}, json={"jsonrpc":"2.0","id":body["id"],"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"mock"}}})
    if method == "tools/list":
        return httpx.Response(200, json={"jsonrpc":"2.0","id":body["id"],"result":{"tools":[{"name":"server_2#query","description":"read only","inputSchema":{"type":"object"}}]}})
    if method == "tools/call":
        return httpx.Response(200, json={"jsonrpc":"2.0","id":body["id"],"result":{"content":[{"type":"text","text":"not json"},{"type":"resource","uri":"/remote/out.json"}],"structuredContent":{"ok":True}}})
    return httpx.Response(200, json={"jsonrpc":"2.0","id":body.get("id"),"result":{}})


def test_mcp_lifecycle_and_multiblock_result():
    client = MCPClient("https://scphub.intern-ai.org.cn/api/v1/mcp/all", api_key="secret", transport=httpx.MockTransport(_transport))
    client.initialize()
    tools = client.list_all_tools()
    assert tools[0].wire_tool_name == "server_2#query"
    obs = client.call_tool("server_2#query", {"q":"x"})
    assert obs.status == "hit" and obs.cache_status == "live"
    assert len(obs.content) == 2 and obs.participates_in_ranking is False

def test_discovery_does_not_fail_when_server_also_exposes_unsafe_tool():
    def mixed(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc":"2.0","id":body["id"],"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{}}}})
        if body["method"] == "tools/list":
            return httpx.Response(200, json={"jsonrpc":"2.0","id":body["id"],"result":{"tools":[{"name":"protocol_generation","inputSchema":{}},{"name":"execute_json","inputSchema":{}}]}})
        return httpx.Response(202)
    client = MCPClient("https://scp.intern-ai.org.cn/api/v1/mcp/19/Thoth-Plan", api_key="secret", transport=httpx.MockTransport(mixed))
    client.initialize()
    assert [tool.name for tool in client.list_all_tools()] == ["protocol_generation", "execute_json"]
    with pytest.raises(PermissionError): validate_outbound_arguments({"api_key":"secret"})


def test_policy_blocks_ssrf_and_skill_code_is_not_executed():
    with pytest.raises(ValueError): validate_endpoint("http://127.0.0.1/mcp")
    with pytest.raises(PermissionError): validate_outbound_arguments({"api_key":"secret"})
    spec, lock = import_skill_markdown("---\nskill_id: x\nservers: [s]\ntools: [t]\n---\n```python\nraise RuntimeError()\n```", repository="https://github.com/InternScience/scp.git", path="skills/x/SKILL.md", commit="abc123", expected_skill_id="x")
    assert spec["skill_id"] == "x" and lock.content_hash.startswith("sha256:")
    assert get_api_key({}) == ("", "missing")

def test_query_cache_and_staging_are_explicit(tmp_path):
    cache = SCPQueryCache(tmp_path)
    observation = SCPObservation(server_id="Scholar-KG", tool_name="query_paper")
    key = cache.key(server_id="Scholar-KG", tool_name="query_paper", schema_hash="sha256:s", arguments={"query":"x"})
    cache.put(key, observation)
    assert cache.get(key)["observation"]["writes_selection"] is False
    staged = cache.stage(observation, session_id="S1", molecule_id="M1", reason="test")
    assert staged["promoted"] is False and cache.list_staging()[0]["stage_id"] == staged["stage_id"]


def test_registry_cache_hit_is_explicit_in_observation_and_audit(tmp_path):
    from types import SimpleNamespace

    cache = SCPQueryCache(tmp_path)
    tool_id = "scp:Scholar-KG:query_paper"
    tool = ToolSpec(
        tool_id=tool_id,
        plugin_id="scp-hub",
        title="query",
        wire_tool_name="query_paper",
        server_id="Scholar-KG",
        descriptor_hash="sha256:schema",
        dynamic=True,
    )
    registry = SimpleNamespace(tools={tool_id: tool})
    manager = SCPRegistryManager(registry, cache=cache)
    session = SimpleNamespace(
        session_id="S-cache",
        installed_scp_skills={
            "literature_research": {
                "enabled": True,
                "tools": [tool_id],
                "servers": [{"server_id": "Scholar-KG", "endpoint": "https://example.test/mcp"}],
            }
        },
    )
    arguments = {"query": "MASLD", "subject": "biology"}
    key = cache.key(
        server_id="Scholar-KG",
        tool_name="query_paper",
        schema_hash="sha256:schema",
        arguments=arguments,
        scope=session.session_id,
    )
    cache.put(
        key,
        SCPObservation(
            server_id="Scholar-KG",
            tool_name="query_paper",
            skill_id="literature_research",
            status="hit",
            cache_status="live",
            response_hash="sha256:response",
        ),
    )

    observation = manager.call(session, tool_id, arguments, allow_live=True)
    assert observation.status == "hit"
    assert observation.cache_status == "cache_hit"
    assert observation.__dict__["cache_status"] == "cache_hit"
    assert observation.response_hash == "sha256:response"
    audit = cache.list_calls(session_id=session.session_id)[0]
    assert audit["status"] == "hit"
    assert audit["cache_status"] == "cache_hit"

def test_restore_session_backfills_tools_without_descriptors():
    from types import SimpleNamespace

    registry = AgentRegistry()
    manager = SCPRegistryManager(registry)
    tool_id = "scp:Scholar-KG:query_paper"
    session = SimpleNamespace(
        installed_scp_skills={
            "literature_research": {
                "skill_id": "literature_research",
                "enabled": True,
                "tools": [tool_id],
                "capability_ids": ["literature_search"],
                # Legacy / partial lock: tools listed but descriptors missing.
            }
        }
    )
    manager.restore_session(session)
    assert tool_id in registry.tools
    assert registry.tools[tool_id].server_id == "Scholar-KG"
    assert registry.tools[tool_id].wire_tool_name == "query_paper"
    assert "literature_research" in registry.skills
    assert registry.skills["literature_research"].tools == [tool_id]


def test_observation_adapter_never_writes_ranking():
    observation = SCPObservation(server_id="SciGraph-Bio", tool_name="query_cypher", response_hash="sha256:abc", identity={"lookup_field":"inchikey","lookup_value":"IK","match_type":"exact_inchikey"})
    hit = observation_to_evidence_hit(observation, molecule_id="M1")
    assert hit.query_status == "hit"
    assert hit.payload["participates_in_ranking"] is False and hit.payload["writes_selection"] is False

def test_long_job_is_session_scoped():
    jobs = SCPJobManager(max_workers=1)
    result = jobs.submit(lambda: SCPObservation(tool_name="protocol_generation"), session_id="S1", skill_id="validation_protocol", tool_id="scp:Thoth-Plan:protocol_generation")
    for _ in range(50):
        current = jobs.get(result["job_id"], session_id="S1")
        if current and current["status"] in {"completed", "failed"}: break
        time.sleep(0.01)
    assert current["status"] == "completed"
    assert current["result"]["cache_status"] == "unknown"
    assert jobs.get(result["job_id"], session_id="S2") is None
