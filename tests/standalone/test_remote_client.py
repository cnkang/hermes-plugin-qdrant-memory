"""The stdio-to-remote adapter uses HTTPS MCP, never the private JSON API."""

import pytest

from qdrant_memory.remote_client import validate_endpoint


@pytest.mark.parametrize(
    "url",
    [
        "http://memory.test/mcp",
        "https://u:p@memory.test/mcp",
        "https://memory.test/mcp?token=x",
        "https://memory.test/mcp#x",
        "https://memory.test/v1/tool",
        "https://memory.test/mcp/",
    ],
)
def test_remote_rejects_unsafe_endpoints(url):
    """Credentials never reach plaintext, embedded credentials or a private API."""
    with pytest.raises(ValueError):
        validate_endpoint(url)


def test_explicit_https_mcp_endpoint():
    """Native and stdio-only clients select the same public transport."""
    assert validate_endpoint("https://memory.example.com/mcp") == "https://memory.example.com/mcp"


def test_legacy_owner_recovery_error_is_normalized_without_replay():
    """A 0.6.0 Owner's typed gate remains terminal and actionable to new clients."""
    import json
    from types import SimpleNamespace

    from qdrant_memory.remote_client import _tool_value

    result = SimpleNamespace(
        content=[
            SimpleNamespace(
                text=json.dumps(
                    {"error": {"type": "SharedRecoveryRequiredError", "retryable": False}}
                )
            )
        ]
    )

    value = _tool_value(result)

    assert value["error"]["reason"] == "recovery_required"
    assert value["error"]["retryable"] is False


def test_production_remote_client_roundtrip_and_fixed_scope(tmp_path, monkeypatch):
    """Real SDK client/server negotiation exercises the adapter's production path."""
    import asyncio
    import ssl

    from qdrant_memory.remote_client import call_remote, http_module
    from qdrant_memory.remote_gateway import create_gateway
    from qdrant_memory.shared_service import SharedOwner

    from ..helpers import runtime
    from .test_device_grants import grant

    rt = runtime(tmp_path)
    token, other = "a" * 40, "b" * 40
    owner = SharedOwner(rt, [grant(token, "mac"), grant(other, "pi", logical="pi", space="pi")])
    app = create_gateway(owner, allowed_hosts=["memory.test"])
    http = http_module()
    real_client = http.AsyncClient

    def local_client(**kwargs):
        # This test covers client protocol logic; separate real TLS tests cover sockets.
        context = kwargs["verify"]
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        return real_client(transport=http.ASGITransport(app=app), **kwargs)

    monkeypatch.setattr(http, "AsyncClient", local_client)

    async def check():
        async with app.router.lifespan_context(app):
            saved = await call_remote(
                "https://memory.test/mcp",
                token,
                "qdrant_memory_add",
                {"text": "cats", "request_id": "sdk-client"},
            )
            replay = await call_remote(
                "https://memory.test/mcp",
                token,
                "qdrant_memory_add",
                {"text": "cats", "request_id": "sdk-client"},
            )
            assert replay == saved
            result = await call_remote(
                "https://memory.test/mcp", other, "qdrant_memory_search", {"query": "cats"}
            )
            assert result["memories"] == []
            with pytest.raises(ValueError):
                await call_remote(
                    "https://memory.test/mcp",
                    token,
                    "qdrant_memory_search",
                    {"query": "cats", "owner_id": "alice"},
                )
            owner.blocked = True
            blocked = await call_remote(
                "https://memory.test/mcp",
                token,
                "qdrant_memory_add",
                {"text": "blocked", "request_id": "recovery-gate"},
            )
            assert blocked["error"]["type"] == "SharedRecoveryRequiredError"
            assert blocked["error"]["reason"] == "recovery_required"
            assert blocked["error"]["retryable"] is False

    try:
        asyncio.run(check())
    finally:
        rt.ledger.close()
        rt.store.close()


@pytest.mark.parametrize("response", [{"status": "PENDING"}, [], {"error": "denied"}])
def test_event_client_retains_uncommitted_event(response, monkeypatch):
    """A syntactically valid response cannot acknowledge uncommitted memory."""
    import asyncio
    import ssl

    import httpx

    from qdrant_memory.remote_client import send_event

    real_client = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["verify"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["verify"].check_hostname is True
        return real_client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)), **kwargs
        )

    monkeypatch.setattr(httpx, "AsyncClient", client)
    prepared = send_event("https://memory.test/mcp", "a" * 40, {"event_id": "synthetic"})
    with pytest.raises(RuntimeError, match="not committed"):
        asyncio.run(prepared)


@pytest.mark.parametrize("status", [200, 400])
def test_control_preserves_only_redacted_machine_error(monkeypatch, status):
    """CLI users can distinguish missing events without reflecting untrusted error text."""
    import asyncio

    import httpx

    from qdrant_memory.remote_client import control_event

    constructor = httpx.AsyncClient
    body = {
        "error": {"type": "KeyError", "message": "PRIVATE", "retryable": False},
        "text": "PRIVATE",
    }
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: constructor(
            transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)), **kwargs
        ),
    )
    result = asyncio.run(control_event("https://memory.test/mcp", "a" * 40, "one", "cancel"))
    assert result["error"]["type"] == "KeyError"
    assert result["error"]["reason"] == "business_error"
    assert result["error"]["retryable"] is False
    assert result["error"]["hint"]
