"""Explicit Owner-side structured LLM adapter for non-Hermes lifecycle capture."""

import math
import os
from types import SimpleNamespace

import httpx

from .config import validate_url


def _reject_non_json_number(value):
    """Reject NaN and Infinity without retaining provider output in the error."""
    raise ValueError("Owner output contains a non-JSON number")


def _finite_json_float(value):
    """Reject exponent overflow as well as explicit non-standard constants."""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Owner output contains a non-finite number")
    return number


class OwnerLLM:
    """Implement the existing Extractor facade using a trusted configured endpoint."""

    def __init__(self, cfg):
        """Require explicit routing; never infer or copy a host's production credentials."""
        if cfg.get("mode") != "override" or cfg.get("provider") != "openai-compatible":
            raise ValueError("Lifecycle Owner requires an explicit openai-compatible LLM override")
        self.model = cfg.get("model")
        self.response_format_mode = cfg.get("response_format_mode", "json_schema")
        if not isinstance(self.response_format_mode, str) or self.response_format_mode not in {
            "json_schema",
            "json_object",
        }:
            raise ValueError("Unsupported Owner response_format_mode")
        self.max_output_tokens = cfg.get("max_output_tokens", 800)
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or not 1 <= self.max_output_tokens <= 8192
        ):
            raise ValueError("Owner output budget must be an integer between 1 and 8192")
        self.reasoning_effort = cfg.get("reasoning_effort")
        self.provider_require_parameters = cfg.get("provider_require_parameters")
        if self.provider_require_parameters is not None and not isinstance(
            self.provider_require_parameters, bool
        ):
            raise ValueError("provider_require_parameters must be a boolean")
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

        # Ground field meanings and numeric bounds in the model's context too.
        # A provider's schema decoder alone need not enforce numeric ranges.
        system = (
            instructions
            + "\n\nReturn JSON that satisfies this schema:\n"
            + json.dumps(json_schema, ensure_ascii=False)
        )
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(input, ensure_ascii=False)},
            ],
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "qdrant_memory", "strict": True, "schema": json_schema},
            },
        }
        if self.response_format_mode == "json_object":
            # Some providers support JSON output without a schema decoder. Keep
            # the full schema in the prompt and enforce it locally before the
            # existing Extractor can admit any memory or acknowledge an event.
            body["response_format"] = {"type": "json_object"}
        if self.reasoning_effort is not None:
            body["reasoning_effort"] = self.reasoning_effort
        if self.provider_require_parameters is not None:
            body["provider"] = {"require_parameters": self.provider_require_parameters}
        response = self.client.post(self.url, json=body)
        if response.status_code != 200:
            raise RuntimeError("Owner extraction provider request failed")
        choice = response.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("Owner extraction output was truncated; retain the event")
        value = choice["message"]["content"]
        if self.response_format_mode == "json_object":
            from jsonschema import Draft202012Validator
            from jsonschema.exceptions import ValidationError
            from referencing import Registry

            failure = None
            try:
                parsed = json.loads(
                    value, parse_constant=_reject_non_json_number, parse_float=_finite_json_float
                )
            except ValueError:
                failure = "Owner output is not valid JSON"
            if failure:
                # Raise after leaving the handler so JSONDecodeError.doc cannot
                # survive through the new exception's implicit context chain.
                raise ValueError(failure)
            try:
                # An explicit empty registry prevents external schema lookups.
                Draft202012Validator(json_schema, registry=Registry()).validate(parsed)
            except ValidationError:
                failure = "Owner output does not satisfy the JSON schema"
            if failure:
                # ValidationError also retains model output. An error raised
                # outside its handler has no provider exception as context.
                raise ValueError(failure)
        else:
            parsed = json.loads(value)
        return SimpleNamespace(parsed=parsed)
