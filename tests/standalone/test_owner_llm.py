"""Explicit model routing uses the existing Extractor contract with safe transport."""

import json

import httpx
import pytest

from qdrant_memory.config import validate_url
from qdrant_memory.extraction import Extractor
from qdrant_memory.owner_llm import OwnerLLM


def test_private_content_transport_requires_tls_without_an_api_key():
    """Anonymous extraction endpoints still receive private content, requiring TLS."""
    with pytest.raises(ValueError, match="HTTPS"):
        validate_url("http://remote.example/v1", require_tls=True)
    validate_url("https://remote.example/v1", require_tls=True)
    validate_url("http://127.0.0.1:11434/v1", require_tls=True)
    validate_url("http://localhost:11434/v1", require_tls=True)


def test_configured_owner_extraction_uses_structured_schema(monkeypatch):
    """Non-Hermes capture does not infer a host account or perform paid model calls."""
    monkeypatch.delenv("QDRANT_MEMORY_LLM_API_KEY", raising=False)
    cfg = {
        "mode": "override",
        "provider": "openai-compatible",
        "model": "fixture",
        "base_url": "http://127.0.0.1:1/v1",
    }
    llm = OwnerLLM(cfg)
    llm.client.close()

    def complete(request):
        body = json.loads(request.content)
        assert body["model"] == "fixture"
        assert body["response_format"]["json_schema"]["strict"] is True
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "memories": [
                                        {
                                            "text": "cats",
                                            "category": ["preference"],
                                            "importance": 0.8,
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    llm.client = httpx.Client(transport=httpx.MockTransport(complete))
    try:
        assert (
            Extractor(llm, cfg).extract({"user": "cats", "assistant": "ack"})[0]["text"] == "cats"
        )
    finally:
        llm.close()


def test_owner_llm_requires_explicit_encrypted_external_configuration():
    """No ambient host inheritance and no plaintext external memory export."""
    with pytest.raises(ValueError):
        OwnerLLM({"mode": "inherit"})
    with pytest.raises(ValueError):
        OwnerLLM(
            {
                "mode": "override",
                "provider": "openai-compatible",
                "model": "x",
                "base_url": "http://remote.example/v1",
            }
        )


def test_truncated_structured_output_is_not_accepted(monkeypatch):
    """Even valid partial JSON must not acknowledge a token-truncated extraction."""
    cfg = {
        "mode": "override",
        "provider": "openai-compatible",
        "model": "fixture",
        "base_url": "http://127.0.0.1:1/v1",
        "max_output_tokens": 2048,
    }
    llm = OwnerLLM(cfg)
    llm.client.close()
    budgets = []

    def response(request):
        budgets.append(json.loads(request.content)["max_tokens"])
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "length", "message": {"content": '{"memories": []}'}}]
            },
        )

    llm.client = httpx.Client(transport=httpx.MockTransport(response))
    try:
        with pytest.raises(ValueError, match="truncated"):
            Extractor(llm, cfg).extract({"user": "anonymous fixture", "assistant": "ack"})
        assert budgets == [2048]
    finally:
        llm.close()


@pytest.mark.parametrize("budget", [0, True, -1, 8193, 1.5])
def test_output_budget_is_explicit_and_bounded(budget):
    """An operator cannot accidentally select an unlimited extraction request."""
    cfg = {
        "mode": "override",
        "provider": "openai-compatible",
        "model": "fixture",
        "base_url": "http://127.0.0.1:1/v1",
        "max_output_tokens": budget,
    }
    with pytest.raises(ValueError):
        llm = OwnerLLM(cfg)
        llm.close()
