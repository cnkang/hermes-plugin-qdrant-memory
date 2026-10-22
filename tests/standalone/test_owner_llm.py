"""Explicit model routing uses the existing Extractor contract with safe transport."""

import json

import httpx
import pytest

from qdrant_memory.config import validate_url
from qdrant_memory.extraction import Extractor
from qdrant_memory.owner_llm import OwnerLLM


@pytest.mark.parametrize("value", ["json", "auto", "", None, [], True])
def test_owner_rejects_unknown_response_format(value):
    """An explicit output mode cannot silently disable schema enforcement."""
    with pytest.raises(ValueError, match="response_format_mode"):
        OwnerLLM(
            {
                "mode": "override",
                "provider": "openai-compatible",
                "model": "fixture",
                "base_url": "http://127.0.0.1:1/v1",
                "response_format_mode": value,
            }
        )


@pytest.mark.parametrize(
    "value,valid",
    [
        ({"ready": True}, True),
        ({"ready": "true"}, False),
        ({"ready": 1}, False),
        ({"ready": True, "extra": "synthetic-private-output"}, False),
        ({}, False),
    ],
)
def test_json_object_mode_enforces_the_same_local_schema(value, valid):
    """JSON-only providers must satisfy types, required fields and extra-field limits."""
    llm = OwnerLLM(
        {
            "mode": "override",
            "provider": "openai-compatible",
            "model": "fixture",
            "base_url": "http://127.0.0.1:1/v1",
            "response_format_mode": "json_object",
        }
    )
    llm.client.close()

    def complete(request):
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert '"additionalProperties": false' in body["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value)}}]})

    llm.client = httpx.Client(transport=httpx.MockTransport(complete))
    schema = {
        "type": "object",
        "properties": {"ready": {"type": "boolean"}},
        "required": ["ready"],
        "additionalProperties": False,
    }
    try:
        if valid:
            assert (
                llm.complete_structured(
                    instructions="Return JSON", input=[], json_schema=schema
                ).parsed
                == value
            )
        else:
            with pytest.raises(ValueError, match="schema") as error:
                llm.complete_structured(instructions="Return JSON", input=[], json_schema=schema)
            assert "synthetic-private-output" not in str(error.value)
            assert error.value.__context__ is None
            assert error.value.__cause__ is None
    finally:
        llm.close()


@pytest.mark.parametrize("importance", [-0.1, 1.1, True, "0.5"])
def test_json_object_mode_preserves_nested_numeric_bounds(importance):
    """A valid JSON object cannot bypass the extraction schema's numeric bounds."""
    cfg = {
        "mode": "override",
        "provider": "openai-compatible",
        "model": "fixture",
        "base_url": "http://127.0.0.1:1/v1",
        "response_format_mode": "json_object",
    }
    llm = OwnerLLM(cfg)
    llm.client.close()
    value = {
        "memories": [{"text": "synthetic", "category": ["preference"], "importance": importance}]
    }
    llm.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps(value)}}]}
            )
        )
    )
    try:
        with pytest.raises(ValueError, match="schema"):
            Extractor(llm, cfg).extract({"user": "synthetic", "assistant": "ack"})
    finally:
        llm.close()


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_json_object_mode_rejects_non_finite_numbers(constant):
    """Python's permissive JSON decoder must not admit non-finite model output."""
    llm = OwnerLLM(
        {
            "mode": "override",
            "provider": "openai-compatible",
            "model": "fixture",
            "base_url": "http://127.0.0.1:1/v1",
            "response_format_mode": "json_object",
        }
    )
    llm.client.close()
    llm.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": '{"score": ' + constant + "}"}}]}
            )
        )
    )
    try:
        with pytest.raises(ValueError):
            llm.complete_structured(
                instructions="Return JSON",
                input=[],
                json_schema={
                    "type": "object",
                    "properties": {"score": {"type": "number"}},
                },
            )
    finally:
        llm.close()


def test_json_object_decode_failure_does_not_retain_provider_exception():
    """A JSONDecodeError would retain the whole model response in its doc field."""
    llm = OwnerLLM(
        {
            "mode": "override",
            "provider": "openai-compatible",
            "model": "fixture",
            "base_url": "http://127.0.0.1:1/v1",
            "response_format_mode": "json_object",
        }
    )
    llm.client.close()
    llm.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"choices": [{"message": {"content": "synthetic-private-output not JSON"}}]},
            )
        )
    )
    try:
        with pytest.raises(ValueError) as error:
            llm.complete_structured(
                instructions="Return JSON", input=[], json_schema={"type": "object"}
            )
        assert type(error.value) is ValueError
        assert "synthetic-private-output" not in str(error.value)
        assert error.value.__context__ is None
        assert error.value.__cause__ is None
        assert not hasattr(error.value, "doc")
    finally:
        llm.close()


@pytest.mark.parametrize(
    "issue",
    [
        None,
        "missing",
        "multiple",
        "wrong_name",
        "wrong_type",
        "not_json",
        "invalid_schema",
        "overflow",
    ],
)
def test_forced_tool_response_is_data_and_must_satisfy_the_schema(issue):
    """Tool-only providers cannot execute tools or bypass the structured result contract."""
    llm = OwnerLLM(
        {
            "mode": "override",
            "provider": "openai-compatible",
            "model": "fixture",
            "base_url": "http://127.0.0.1:1/v1",
            "response_format_mode": "tool_call",
        }
    )
    llm.client.close()
    schema = {
        "type": "object",
        "properties": {"ready": {"type": "boolean"}},
        "required": ["ready"],
        "additionalProperties": False,
    }

    def complete(request):
        body = json.loads(request.content)
        assert "response_format" not in body
        assert body["tool_choice"] == {"type": "function", "function": {"name": "qdrant_memory"}}
        assert body["tools"][0]["function"]["parameters"] == schema
        call = {
            "type": "function",
            "function": {"name": "qdrant_memory", "arguments": '{"ready":true}'},
        }
        if issue == "wrong_name":
            call["function"]["name"] = "arbitrary_tool"
        if issue == "wrong_type":
            call["type"] = "custom"
        if issue == "not_json":
            call["function"]["arguments"] = "synthetic-private-output"
        if issue == "invalid_schema":
            call["function"]["arguments"] = '{"ready":"synthetic-private-output"}'
        if issue == "overflow":
            call["function"]["arguments"] = '{"ready":1e999}'
        calls = [] if issue == "missing" else [call, call] if issue == "multiple" else [call]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {"content": None, "tool_calls": calls},
                    }
                ]
            },
        )

    llm.client = httpx.Client(transport=httpx.MockTransport(complete))
    try:
        if issue is None:
            assert llm.complete_structured(
                instructions="Return structured data", input=[], json_schema=schema
            ).parsed == {"ready": True}
        else:
            with pytest.raises(ValueError) as error:
                llm.complete_structured(
                    instructions="Return structured data", input=[], json_schema=schema
                )
            assert "synthetic-private-output" not in str(error.value)
            assert error.value.__context__ is None
    finally:
        llm.close()


@pytest.fixture(autouse=True)
def isolate_provider_credentials(monkeypatch):
    """Mock-provider tests never inherit a real optional account credential."""
    monkeypatch.delenv("QDRANT_MEMORY_LLM_API_KEY", raising=False)


def test_private_content_transport_requires_tls_without_an_api_key():
    """Anonymous extraction endpoints still receive private content, requiring TLS."""
    with pytest.raises(ValueError, match="HTTPS"):
        validate_url("http://remote.example/v1", require_tls=True)
    validate_url("https://remote.example/v1", require_tls=True)
    validate_url("http://127.0.0.1:11434/v1", require_tls=True)
    validate_url("http://localhost:11434/v1", require_tls=True)


@pytest.mark.parametrize("required", [None, True, False])
def test_configured_owner_extraction_uses_structured_schema(monkeypatch, required):
    """Non-Hermes capture does not infer a host account or perform paid model calls."""
    monkeypatch.delenv("QDRANT_MEMORY_LLM_API_KEY", raising=False)
    cfg = {
        "mode": "override",
        "provider": "openai-compatible",
        "model": "fixture",
        "base_url": "http://127.0.0.1:1/v1",
        "provider_require_parameters": required,
    }
    llm = OwnerLLM(cfg)
    llm.client.close()

    def complete(request):
        body = json.loads(request.content)
        assert body["model"] == "fixture"
        assert body["response_format"]["json_schema"]["strict"] is True
        schema = body["response_format"]["json_schema"]["schema"]
        assert json.dumps(schema, ensure_ascii=False) in body["messages"][0]["content"]
        assert body["messages"][0]["content"].startswith("Extract durable facts")
        assert json.loads(body["messages"][1]["content"]) == [
            {"type": "text", "text": json.dumps({"user": "cats", "assistant": "ack"})}
        ]
        assert body.get("provider") == (
            None if required is None else {"require_parameters": required}
        )
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


@pytest.mark.parametrize("required", [0, "true", []])
def test_owner_rejects_invalid_provider_parameter_policy(required):
    """Provider routing options are explicit booleans, not arbitrary request fragments."""
    with pytest.raises(ValueError, match="provider_require_parameters"):
        OwnerLLM(
            {
                "mode": "override",
                "provider": "openai-compatible",
                "model": "fixture",
                "base_url": "http://127.0.0.1:1/v1",
                "provider_require_parameters": required,
            }
        )


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


@pytest.mark.parametrize("effort", [None, "none", "low"])
def test_reasoning_effort_requires_explicit_configuration(effort):
    """Preserve provider defaults unless an operator selects a supported control."""
    cfg = {
        "mode": "override",
        "provider": "openai-compatible",
        "model": "fixture",
        "base_url": "http://127.0.0.1:1/v1",
    }
    if effort is not None:
        cfg["reasoning_effort"] = effort
    llm = OwnerLLM(cfg)
    llm.client.close()
    requests = []

    def response(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"memories": []}'}}]})

    llm.client = httpx.Client(transport=httpx.MockTransport(response))
    try:
        assert Extractor(llm, cfg).extract({"user": "synthetic", "assistant": "ack"}) == []
        if effort is None:
            assert "reasoning_effort" not in requests[0]
        else:
            assert requests[0]["reasoning_effort"] == effort
        assert requests[0]["response_format"]["json_schema"]["strict"] is True
        assert llm.client.follow_redirects is False
    finally:
        llm.close()


@pytest.mark.parametrize("effort", [True, {}, [], 0, "unsupported"])
def test_invalid_reasoning_effort_fails_before_provider_io(effort):
    """Malformed inference options must fail without changing transport policy."""
    with pytest.raises(ValueError, match="reasoning"):
        llm = OwnerLLM(
            {
                "mode": "override",
                "provider": "openai-compatible",
                "model": "fixture",
                "base_url": "http://127.0.0.1:1/v1",
                "reasoning_effort": effort,
            }
        )
        llm.close()
