"""Failed-relevance SCP replies should still organize retrieved materials."""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent.registry import get_registry
from agent.runtime.loop import AgentRuntime
from agent.runtime.task_router import TaskRouter


def _runtime_stub() -> AgentRuntime:
    rt = AgentRuntime.__new__(AgentRuntime)
    rt.store = SimpleNamespace(persist=lambda session: None)
    rt.registry = get_registry()
    rt.task_router = TaskRouter(rt.registry)
    return rt


def test_format_scp_evidence_materials_lists_papers() -> None:
    payload = {
        "output": [
            {
                "paper_title": "PPAR-alpha signaling in retinal lipid metabolism",
                "pub_year": 2021,
                "authors": ["Alice", "Bob"],
                "abstract": "Retinal PPAR pathways regulate lipid handling.",
                "doi": "10.1000/example",
            },
            {
                "title": "Hepatic PPAR notes",
                "year": 2019,
                "summary": "Brief note without MASLD framing.",
            },
        ]
    }
    text = AgentRuntime._format_scp_evidence_materials([json.dumps(payload)])
    assert "## 检索到的资料" in text
    assert "PPAR-alpha signaling in retinal lipid metabolism（2021）" in text
    assert "作者：Alice、Bob" in text
    assert "DOI：10.1000/example" in text
    assert "Hepatic PPAR notes（2019）" in text


def test_failed_relevance_reply_shows_materials_before_conclusion(monkeypatch) -> None:
    rt = _runtime_stub()
    monkeypatch.setenv("MOLMIND_LLM_CHAT", "0")
    payload = {
        "output": [
            {
                "paper_title": "PPAR-alpha in retinal metabolism",
                "pub_year": 2020,
                "abstract": "Focuses on retina rather than MASLD.",
            }
        ]
    }
    reply = rt._synthesize_scp_reply(
        question="检索 PPAR 与脂肪肝相关论文，允许使用实时资料",
        label="科研文献检索",
        values=[json.dumps(payload, ensure_ascii=False)],
        digest={
            "relevance": {
                "relevant": False,
                "missing_concepts": ["MASLD"],
                "reasons": [
                    "missing_concept:MASLD",
                    "excluded_concept_present:retina",
                ],
            }
        },
    )
    materials_at = reply.index("## 检索到的资料")
    conclusion_at = reply.index("## 相关性校验结论")
    assert materials_at < conclusion_at
    assert "PPAR-alpha in retinal metabolism（2020）" in reply
    assert "代谢相关脂肪性肝病" in reply or "MASLD" in reply
    assert "已排除主题" in reply and "视网膜" in reply
    assert "不参与候选排序" in reply
    assert reply.index("检索到的资料") < reply.index("未通过相关性校验")
