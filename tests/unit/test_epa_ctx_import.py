from __future__ import annotations

from urllib.error import HTTPError
from types import SimpleNamespace

import pytest

from services.public_data.epa_ctx_bundle import CtxClient, map_candidate, response_count
import services.public_data.epa_ctx_bundle as epa_ctx_bundle
from plugins.molmind_core.scientific.evidence_gateway import credentials
from plugins.molmind_core.tools import evidence_query


class FakeClient:
    def search_exact(self, value: str):
        if value == "IK-EXACT":
            return [{"dtxsid": "DTXSID1", "dtxcid": "DTXCID1", "casrn": "1-11-1", "preferredName": "X", "smiles": "CC"}]
        return []


def test_mapping_prefers_original_inchikey() -> None:
    result = map_candidate(FakeClient(), {"molecule_id": "T1", "original_inchikey": "IK-EXACT", "cas": "1-11-1"})
    assert result["dtxsid"] == "DTXSID1"
    assert result["mapping_status"] == "exact_identifier_match"
    assert result["mapping_basis"] == "original_inchikey"


def test_mapping_tries_standardized_before_cas() -> None:
    class Client:
        def search_exact(self, value: str):
            if value == "IK-STD":
                return [
                    {
                        "dtxsid": "DTXSID2",
                        "dtxcid": "DTXCID2",
                        "casrn": "2-22-2",
                        "preferredName": "Y",
                        "smiles": "CCO",
                    }
                ]
            if value == "2-22-2":
                return [
                    {
                        "dtxsid": "DTXSID-CAS",
                        "dtxcid": "DTXCID-CAS",
                        "casrn": "2-22-2",
                        "preferredName": "CasHit",
                        "smiles": "C",
                    }
                ]
            return []

    result = map_candidate(
        Client(),
        {
            "molecule_id": "T2",
            "original_inchikey": "IK-BAD",
            "standardized_inchikey": "IK-STD",
            "cas": "2-22-2",
        },
    )
    assert result["dtxsid"] == "DTXSID2"
    assert result["mapping_basis"] == "standardized_inchikey"
    assert result["mapping_status"] == "exact_identifier_match"


def test_empty_response_is_zero_not_negative_label() -> None:
    assert response_count([]) == 0
    assert response_count({}) == 0


def test_mounted_secret_resolves_api_key(monkeypatch, tmp_path) -> None:
    secret = tmp_path / "ctx-api-key"
    secret.write_text("test-mounted-key\n", encoding="utf-8")
    monkeypatch.delenv("CTX_API_KEY", raising=False)
    monkeypatch.setenv("CTX_API_KEY_FILE", str(secret))
    client = CtxClient(api_key="")
    assert client.api_key == "test-mounted-key"


def test_public_embedded_ctx_key_is_default_and_can_be_disabled(monkeypatch) -> None:
    for name in ("CTX_API_KEY", "CCTE_API_KEY", "MOLMIND_CTX_API_KEY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)
    monkeypatch.delenv("MOLMIND_EPA_CTX_SECRET_FILE", raising=False)
    monkeypatch.setattr(credentials, "_macos_keychain", lambda *_args: None)

    value = credentials.resolve_secret("epa_ctx", env_names=("CTX_API_KEY",))
    assert value is not None
    assert len(value) == 36

    monkeypatch.setenv("MOLMIND_USE_EMBEDDED_PUBLIC_KEYS", "0")
    assert credentials.resolve_secret("epa_ctx", env_names=("CTX_API_KEY",)) is None


def test_ctx_client_does_not_retry_deterministic_client_errors(monkeypatch) -> None:
    calls = 0

    def reject(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise HTTPError("https://example.test", 400, "bad request", {}, None)

    monkeypatch.setattr(epa_ctx_bundle, "urlopen", reject)
    client = CtxClient(api_key="test-key", retries=3)
    with pytest.raises(HTTPError):
        client.get_json("chemical/search/equal/IK")
    assert calls == 1


def test_ctx_client_uses_configured_provider_base_url(monkeypatch) -> None:
    seen: list[str] = []

    def reject(request, **_kwargs):
        seen.append(request.full_url)
        raise HTTPError(request.full_url, 404, "not found", {}, None)

    monkeypatch.setattr(epa_ctx_bundle, "urlopen", reject)
    client = CtxClient(
        api_key="test-key",
        retries=1,
        base_url="https://ctx.example.test/api/",
    )
    with pytest.raises(HTTPError):
        client.search_exact("64-17-5")
    assert seen == [
        "https://ctx.example.test/api/chemical/search/equal/64-17-5?projection=chemicalsearchall"
    ]


def test_live_epa_strong_cytotox_is_exact_risk_only(monkeypatch) -> None:
    class Client:
        def __init__(self, **_kwargs):
            pass

        def search_exact(self, _value):
            return [{"dtxsid": "DTXSID1", "preferredName": "candidate"}]

    monkeypatch.setattr(evidence_query, "CtxClient", Client)
    monkeypatch.setattr(
        evidence_query,
        "query_candidate",
        lambda *_args: {"responses": {"bioactivity_summary": [{"nhit": 2, "cytotoxLowerUm": 4.0}]}, "errors": []},
    )
    hits = evidence_query._epa_identity_adapter(
        SimpleNamespace(lookup_value="IK-EXACT", lookup_field="original_inchikey", timeout_sec=2),
        {"api_key": "test-key", "api_base": "https://ctx.example.test"},
        risk_policy={"cytotox_screening_um": 10, "max_risk_score": 0.4, "risk_confidence": 0.5},
    )
    risk = next(hit for hit in hits if hit.query_type == "tox")
    assert risk.adapter_id == "epa_ctx_tox_v1"
    assert risk.score == 0.4
    assert risk.claim_ceiling.startswith("candidate_risk_signal")
