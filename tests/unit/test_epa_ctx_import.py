from __future__ import annotations

from urllib.error import HTTPError

import pytest

from services.public_data.epa_ctx_bundle import CtxClient, map_candidate, response_count
import services.public_data.epa_ctx_bundle as epa_ctx_bundle


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


def test_project_config_resolves_api_key(monkeypatch) -> None:
    monkeypatch.delenv("CTX_API_KEY", raising=False)
    client = CtxClient(api_key="")
    assert client.api_key


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
