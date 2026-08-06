"""机制 LLM：OpenAI 兼容客户端 + 不改排名 + 无 Key 模板降级。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from packages.models import Attribution, ScoreRecord
from services.mechanism.llm_client import (
    MechanismLLMError,
    chat_completion,
    resolve_llm_settings,
)
from services.mechanism.mechanism import render_mechanism_markdown
from services.pipeline.config_loader import load_config


def _mol(mid: str = "T001") -> ScoreRecord:
    return ScoreRecord(
        molecule_id=mid,
        smiles="CCO",
        inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
        cas=None,
        scaffold_smiles="CCO",
        lipid_score=0.6,
        tox_risk=0.2,
        novelty_score=0.5,
        conf_e=0.4,
        final_score=0.55,
        tox_heads={},
        lipid_parts={},
        attributions=[
            Attribution("evidence", "chembl", value=0.5, evidence_id="chembl:X1")
        ],
        lipid_rationale="rule+evidence",
        tox_rationale="low alert",
        overall_reason="ok",
        toxicity_confidence=0.8,
        toxicity_uncertainty=0.2,
        eligibility_status="eligible",
        eligibility_reasons=("lipid_and_toxicity_policy_passed",),
    )


def test_resolve_settings_reads_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MOLMIND_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("MOLMIND_LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("MOLMIND_LLM_MECHANISM", "1")
    s = resolve_llm_settings(
        {
            "enabled": False,
            "mechanism_pdf": True,
            "model": "ignored",
            "base_url": "https://api.deepseek.com/v1",
            "cache_dir": str(tmp_path),
            "temperature": 0,
        }
    )
    assert s.enabled is True
    assert s.ready is True
    assert s.model == "deepseek-chat"
    assert s.api_key == "sk-test"


def test_chat_completion_uses_cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MOLMIND_LLM_API_KEY", "sk-test")
    settings = resolve_llm_settings(
        {
            "enabled": True,
            "mechanism_pdf": True,
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com/v1",
            "cache_dir": str(tmp_path),
            "use_cache": True,
            "temperature": 0,
        }
    )
    # 预写缓存
    from services.mechanism.llm_client import _cache_key, _write_cache

    key = _cache_key("deepseek-chat", "sys", "user", 0.0)
    _write_cache(tmp_path, key, model="deepseek-chat", content="cached-body")
    text = chat_completion(settings, system="sys", user="user")
    assert text == "cached-body"


def test_chat_completion_http(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MOLMIND_LLM_API_KEY", "sk-live")
    settings = resolve_llm_settings(
        {
            "enabled": True,
            "mechanism_pdf": True,
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com/v1",
            "cache_dir": str(tmp_path),
            "use_cache": True,
            "temperature": 0,
        }
    )
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {
        "choices": [{"message": {"content": "### 机制假说\n测试正文"}}]
    }
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.post.return_value = fake_resp

    with patch("services.mechanism.llm_client.httpx.Client", return_value=fake_client):
        text = chat_completion(settings, system="s", user="u")
    assert "机制假说" in text
    # 第二次应走缓存，不再 post
    fake_client.post.reset_mock()
    with patch("services.mechanism.llm_client.httpx.Client", return_value=fake_client):
        text2 = chat_completion(settings, system="s", user="u")
    assert text2 == text
    fake_client.post.assert_not_called()


def test_chat_completion_stream_yields_deltas(monkeypatch, tmp_path: Path) -> None:
    from services.mechanism.llm_client import chat_completion_stream

    monkeypatch.setenv("MOLMIND_LLM_API_KEY", "sk-live")
    settings = resolve_llm_settings(
        {
            "enabled": True,
            "mechanism_pdf": True,
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com/v1",
            "cache_dir": str(tmp_path),
            "use_cache": False,
            "temperature": 0,
        }
    )

    sse_body = (
        'data: {"choices":[{"delta":{"content":"你"}}]}\n'
        'data: {"choices":[{"delta":{"content":"好"}}]}\n'
        "data: [DONE]\n"
    )

    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.iter_text.return_value = iter([sse_body])
    fake_resp.read = MagicMock()
    fake_resp.__enter__ = MagicMock(return_value=fake_resp)
    fake_resp.__exit__ = MagicMock(return_value=False)

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.stream.return_value = fake_resp

    with patch("services.mechanism.llm_client.httpx.Client", return_value=fake_client):
        chunks = list(chat_completion_stream(settings, system="s", user="u"))
    assert chunks == ["你", "好"]
    fake_client.stream.assert_called_once()
    _, kwargs = fake_client.stream.call_args
    assert kwargs["json"]["stream"] is True


def test_chat_completion_stream_uses_cache(monkeypatch, tmp_path: Path) -> None:
    from services.mechanism.llm_client import (
        _cache_key,
        _write_cache,
        chat_completion_stream,
    )

    monkeypatch.setenv("MOLMIND_LLM_API_KEY", "sk-test")
    settings = resolve_llm_settings(
        {
            "enabled": True,
            "mechanism_pdf": True,
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com/v1",
            "cache_dir": str(tmp_path),
            "use_cache": True,
            "temperature": 0,
        }
    )
    key = _cache_key("deepseek-chat", "sys", "user", 0.0)
    _write_cache(tmp_path, key, model="deepseek-chat", content="cached-stream")
    chunks = list(chat_completion_stream(settings, system="sys", user="user"))
    assert chunks == ["cached-stream"]


def test_render_falls_back_without_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MOLMIND_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MOLMIND_LLM_MECHANISM", raising=False)
    monkeypatch.setenv("MOLMIND_LLM_USE_EMBEDDED", "0")
    path = tmp_path / "mech.md"
    render_mechanism_markdown(
        [_mol()],
        path,
        llm_cfg={
            "enabled": True,
            "mechanism_pdf": True,
            "model": "deepseek-v4-pro",
            "base_url": "https://api.deepseek.com/v1",
            "temperature": 0,
            "cache_dir": str(tmp_path / "cache"),
        },
    )
    text = path.read_text(encoding="utf-8")
    assert "假设通路" in text
    assert "活力 >=80%" in text
    assert "准确模板" in text


def test_render_llm_does_not_mutate_scores(tmp_path: Path, monkeypatch) -> None:
    """即使开启 LLM 配置，机制正文仍为准确模板，且不改分数。"""
    monkeypatch.setenv("MOLMIND_LLM_API_KEY", "sk-test")
    mol = _mol("T9")
    before = (mol.final_score, mol.lipid_score, mol.tox_risk)
    path = tmp_path / "out.md"
    render_mechanism_markdown(
        [mol],
        path,
        llm_cfg={
            "enabled": True,
            "mechanism_pdf": True,
            "model": "deepseek-v4-pro",
            "base_url": "https://api.deepseek.com/v1",
            "temperature": 0,
            "use_cache": True,
            "cache_dir": str(tmp_path / "c"),
        },
    )
    assert (mol.final_score, mol.lipid_score, mol.tox_risk) == before
    text = path.read_text(encoding="utf-8")
    assert "准确模板" in text
    assert "排名已冻结" in text
    assert "活力 >=80%" in text
    assert "毒理/警示证据 ID" in text


def test_chat_not_ready_raises(monkeypatch) -> None:
    monkeypatch.setenv("MOLMIND_LLM_USE_EMBEDDED", "0")
    monkeypatch.delenv("MOLMIND_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    s = resolve_llm_settings({"enabled": True, "mechanism_pdf": True, "model": "x"})
    assert s.ready is False
    try:
        chat_completion(s, system="a", user="b")
        raise AssertionError("expected MechanismLLMError")
    except MechanismLLMError:
        pass


def test_config_llm_mechanism_on_critic_off() -> None:
    cfg = load_config(mode="offline")
    # 机制 PDF 默认准确模板，不走 LLM 自由撰写
    assert cfg.llm_mechanism_enabled is False
    assert cfg.llm_critic_enabled is False
    assert cfg.llm_critic_affects_ranking is False
    assert cfg.llm.get("model") == "deepseek-v4-pro"


def test_embedded_key_ready_without_env(monkeypatch) -> None:
    monkeypatch.delenv("MOLMIND_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("MOLMIND_LLM_USE_EMBEDDED", "1")
    s = resolve_llm_settings(
        {
            "enabled": True,
            "mechanism_pdf": True,
            "model": "deepseek-v4-pro",
            "base_url": "https://api.deepseek.com",
            "temperature": 0,
        }
    )
    assert s.ready is True
    assert s.api_key.startswith("sk-")
    assert s.base_url == "https://api.deepseek.com/v1"
    assert s.model == "deepseek-v4-pro"
