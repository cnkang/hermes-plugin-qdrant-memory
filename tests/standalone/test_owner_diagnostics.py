"""Transport diagnostics and extraction budget stay actionable and bounded."""

import ssl

import pytest

from qdrant_memory.config import DEFAULTS, merge, validate_llm
from qdrant_memory.extraction import Extractor
from qdrant_memory.remote_client import remote_error, send_event

from ..helpers import LLM


def test_remote_error_unwraps_groups_and_names_the_cause():
    """A task group must not hide the transport cause from the operator."""
    grouped = ExceptionGroup("unhandled", [ssl.SSLCertVerificationError("secret host")])
    error = remote_error(grouped)
    assert error["type"] == "SSLCertVerificationError"
    assert error["reason"] == "tls_verification_failed"
    assert error["retryable"] is False
    assert "secret" not in error["hint"].lower()

    from qdrant_memory.remote_client import RemoteStatusError

    assert remote_error(RemoteStatusError(401))["reason"] == "authentication_rejected"
    import ssl as ssl_module

    wrapped = ConnectionError("outer")
    wrapped.__cause__ = ssl_module.SSLCertVerificationError("inner")
    assert remote_error(wrapped)["reason"] == "tls_verification_failed"
    assert remote_error(TimeoutError("secret host"))["reason"] == "timeout"
    assert remote_error(ConnectionError("secret host"))["reason"] == "unreachable"


def test_remote_error_maps_status_codes_without_remote_text():
    """Authentication and business failures keep distinct, redacted codes."""
    import httpx

    request = httpx.Request("POST", "https://owner.test/mcp")
    unauthorized = httpx.HTTPStatusError(
        "secret body", request=request, response=httpx.Response(401, request=request)
    )
    error = remote_error(ExceptionGroup("unhandled", [unauthorized]))
    assert error["reason"] == "authentication_rejected"
    assert "secret" not in str(error)
    conflict = httpx.HTTPStatusError(
        "secret body", request=request, response=httpx.Response(409, request=request)
    )
    assert remote_error(conflict)["reason"] == "business_error"


@pytest.mark.anyio
async def test_send_event_accepts_a_quarantined_receipt(monkeypatch):
    """A retained event is a durable acknowledgement, not a retry loop."""
    import qdrant_memory.remote_client as module

    async def receipt(*_args, **_kwargs):
        return {"event_id": "turn-1", "status": "QUARANTINED"}

    monkeypatch.setattr(module, "_event_request", receipt)
    result = await send_event("https://owner.test/mcp", "a" * 40, {"event_id": "turn-1"})
    assert result["status"] == "QUARANTINED"


def test_output_budget_is_configurable_and_bounded():
    """The default stays 800 and an explicit larger budget reaches the model call."""
    assert DEFAULTS["llm"]["max_output_tokens"] == 800
    validate_llm({"mode": "inherit", "max_output_tokens": 2048})
    with pytest.raises(ValueError):
        validate_llm({"mode": "inherit", "max_output_tokens": 100})
    with pytest.raises(ValueError):
        validate_llm({"mode": "inherit", "max_output_tokens": 9000})

    llm = LLM()
    cfg = merge(DEFAULTS, {"llm": {"max_output_tokens": 2048}})
    Extractor(llm, cfg["llm"]).extract({"user": "synthetic fact", "assistant": "ack"})
    assert llm.calls[0]["max_tokens"] == 2048
