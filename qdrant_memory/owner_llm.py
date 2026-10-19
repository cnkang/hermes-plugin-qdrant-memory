"""Explicit Owner-side structured LLM adapter for non-Hermes lifecycle capture."""

import os
from types import SimpleNamespace

import httpx

from .config import validate_url


class OwnerLLM:
    """Implement the existing Extractor facade using a trusted configured endpoint."""

    def __init__(self, cfg):
        """Require explicit routing; never infer or copy a host's production credentials."""
        if cfg.get("mode") != "override" or cfg.get("provider") != "openai-compatible":
            raise ValueError("Lifecycle Owner requires an explicit openai-compatible LLM override")
        self.model = cfg.get("model")
        self.url = cfg.get("base_url", "").rstrip("/") + "/chat/completions"
        key = os.environ.get(cfg.get("api_key_env", "QDRANT_MEMORY_LLM_API_KEY"))
        validate_url(self.url, api_key=key or "require-encrypted-external-transport")
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("An explicit extraction model is required")
        headers = {"Authorization": "Bearer " + key} if key else {}
        self.client = httpx.Client(
            headers=headers, trust_env=False, follow_redirects=False, timeout=30
        )

    def close(self):
        """Release the Owner's HTTP pool after all admitted lifecycle work drains."""
        self.client.close()

    def complete_structured(self, *, instructions, input, json_schema, **kwargs):
        """Return structured output to the existing Extractor validation pipeline."""
        import json

        response = self.client.post(
            self.url,
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": json.dumps(input, ensure_ascii=False)},
                ],
                "temperature": 0,
                "max_tokens": 800,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "qdrant_memory", "strict": True, "schema": json_schema},
                },
            },
        )
        if response.status_code != 200:
            raise RuntimeError("Owner extraction provider request failed")
        value = response.json()["choices"][0]["message"]["content"]
        return SimpleNamespace(parsed=json.loads(value))
