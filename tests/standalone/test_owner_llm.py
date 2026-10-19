"""Explicit model routing uses the existing Extractor contract with safe transport."""

import json

import httpx
import pytest

from qdrant_memory.extraction import Extractor
from qdrant_memory.owner_llm import OwnerLLM


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
