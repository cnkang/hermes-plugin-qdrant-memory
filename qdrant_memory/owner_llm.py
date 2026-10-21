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
        self.max_output_tokens = cfg.get("max_output_tokens", 800)
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or not 1 <= self.max_output_tokens <= 8192
        ):
            raise ValueError("Owner output budget must be an integer between 1 and 8192")
        self.reasoning_effort = cfg.get("reasoning_effort")
        if self.reasoning_effort is not None and (
            not isinstance(self.reasoning_effort, str)
            or self.reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}
        ):
            raise ValueError("Unsupported Owner reasoning effort")
        self.url = cfg.get("base_url", "").rstrip("/") + "/chat/completions"
        key = os.environ.get(cfg.get("api_key_env", "QDRANT_MEMORY_LLM_API_KEY"))
        validate_url(self.url, api_key=key, require_tls=True)
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

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(input, ensure_ascii=False)},
            ],
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "qdrant_memory", "strict": True, "schema": json_schema},
            },
        }
        if self.reasoning_effort is not None:
            body["reasoning_effort"] = self.reasoning_effort
        response = self.client.post(self.url, json=body)
        if response.status_code != 200:
            raise RuntimeError("Owner extraction provider request failed")
        choice = response.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("Owner extraction output was truncated; retain the event")
        value = choice["message"]["content"]
        return SimpleNamespace(parsed=json.loads(value))
