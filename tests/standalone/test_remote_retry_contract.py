"""A restart cut must stay replayable instead of dropping an admitted write."""

import asyncio
import ssl

import httpx
import pytest

from qdrant_memory.remote_client import (
    RemoteStatusError,
    call_remote,
    control_event,
    http_module,
    remote_error,
    send_event,
)

from ..helpers import runtime
from .test_device_grants import grant

TOKEN = "a" * 40
HTTP = http_module()


def _mcp_error(code):
    """Build one official SDK protocol error with a fixed error code."""
    import mcp.shared.exceptions as exceptions
    from mcp.types import ErrorData

    legacy = getattr(exceptions, "McpError", None)
    if legacy is not None:
        return legacy(ErrorData(code=code, message="synthetic"))
    return exceptions.MCPError(code, "synthetic")


@pytest.mark.parametrize(
    ("exc", "reason", "retryable"),
    [
        (httpx.ConnectError("synthetic"), "unreachable", True),
        (httpx.ConnectTimeout("synthetic"), "unreachable", True),
        (httpx.ReadTimeout("synthetic"), "timeout", True),
        (httpx.WriteTimeout("synthetic"), "timeout", True),
        (httpx.RemoteProtocolError("synthetic"), "outcome_unknown", True),
        (httpx.ReadError("synthetic"), "outcome_unknown", True),
        (httpx.WriteError("synthetic"), "outcome_unknown", True),
        (ConnectionResetError("synthetic"), "outcome_unknown", True),
        (ConnectionAbortedError("synthetic"), "outcome_unknown", True),
        (BrokenPipeError("synthetic"), "outcome_unknown", True),
        (TimeoutError("synthetic"), "timeout", True),
        (ConnectionError("synthetic"), "unreachable", True),
        (ssl.SSLCertVerificationError("synthetic"), "tls_verification_failed", False),
        (ssl.SSLError("synthetic"), "tls_failed", False),
        (PermissionError("Remote MCP redirects are forbidden"), "protocol_error", False),
        (RemoteStatusError(302), "protocol_error", False),
        (
            ExceptionGroup(
                "unhandled",
                [
                    PermissionError("Remote MCP redirects are forbidden"),
                    httpx.ReadError("synthetic"),
                ],
            ),
            "protocol_error",
            False,
        ),
        (RemoteStatusError(401), "authentication_rejected", False),
        (RemoteStatusError(403), "authentication_rejected", False),
        (RemoteStatusError(408), "temporarily_unavailable", True),
        (RemoteStatusError(429), "temporarily_unavailable", True),
        (RemoteStatusError(502), "temporarily_unavailable", True),
        (RemoteStatusError(503), "temporarily_unavailable", True),
        (RemoteStatusError(500), "business_error", False),
        (RemoteStatusError(400, error_type="ValueError", retryable=False), "business_error", False),
        (
            RemoteStatusError(None, error_type="SharedRecoveryRequiredError", retryable=False),
            "recovery_required",
            False,
        ),
        (
            RemoteStatusError(None, error_type="SharedAdmissionError", retryable=True),
            "temporarily_unavailable",
            True,
        ),
    ],
)
def test_transport_failures_classify_for_replay(exc, reason, retryable):
    """Every transport and status class keeps one redacted, stable verdict."""
    error = remote_error(exc)
    assert error["reason"] == reason
    assert error["retryable"] is retryable


def _legacy_stream_error():
    """Return the SDK 1.x class name with the code under ``error``."""

    class McpError(Exception):
        """Stand in for one SDK 1.x transport error."""

        def __init__(self):
            """Keep the connection-closed code in the SDK 1.x location."""
            super().__init__("synthetic")
            self.error = type("ErrorData", (), {"code": -32000})()

    return McpError()


def _current_stream_error():
    """Return the SDK 2.x class name with the code on the exception."""

    class MCPError(Exception):
        """Stand in for one SDK 2.x transport error."""

        def __init__(self):
            """Keep the connection-closed code on the exception itself."""
            super().__init__("synthetic")
            self.code = -32000

    return MCPError()


@pytest.mark.parametrize(
    "exc",
    [
        _mcp_error(-32000),
        _legacy_stream_error(),
        _current_stream_error(),
        ExceptionGroup("unhandled", [ValueError("synthetic"), httpx.ReadError("synthetic")]),
    ],
)
def test_interrupted_streams_are_indeterminate(exc):
    """A closed MCP stream or a nested group must resolve to replayable state."""
    error = remote_error(exc)
    assert error["reason"] == "outcome_unknown"
    assert error["retryable"] is True


def test_deterministic_protocol_error_is_not_replayable():
    """A refused method is a determinate result, not an ambiguous write."""
    error = remote_error(_mcp_error(-32601))
    assert error["reason"] == "protocol_error"
    assert error["retryable"] is False


def test_replay_field_names_the_idempotency_key():
    """Clients learn which identifier to replay and never invent a second one."""
    cut = httpx.RemoteProtocolError("synthetic")
    assert remote_error(cut, replay="request_id")["replay"] == "request_id"
    assert remote_error(cut, replay="event_id")["replay"] == "event_id"
    assert "replay" not in remote_error(RemoteStatusError(403), replay="request_id")


class DropResponse(HTTP.AsyncBaseTransport):
    """Run the real request, then lose the response of the first N calls."""

    def __init__(self, inner, drops, match=b""):
        """Wrap one transport and record how often the matched call arrived."""
        self.inner = inner
        self.drops = drops
        self.match = match
        self.attempts = 0

    async def handle_async_request(self, request):
        """Commit service-side work, then raise as a cut connection would."""
        body = getattr(request, "content", b"") or b""
        matched = self.match in body
        response = await self.inner.handle_async_request(request)
        if matched:
            self.attempts += 1
            if self.attempts <= self.drops:
                await response.aread()
                raise httpx.ReadError("synthetic response loss", request=request)
        return response


def _shared_owner(tmp_path):
    """Build one real Owner with a writable device grant."""
    rt = runtime(tmp_path)
    return rt, grant(TOKEN, "mac")


def test_lost_tool_response_replays_to_the_same_record(tmp_path, monkeypatch):
    """A committed write with a lost response must not become a second memory."""
    from qdrant_memory.remote_gateway import create_gateway
    from qdrant_memory.shared_service import SharedOwner

    rt, device = _shared_owner(tmp_path)
    app = create_gateway(SharedOwner(rt, [device]), allowed_hosts=["memory.test"])
    constructor = HTTP.AsyncClient
    dropper = DropResponse(HTTP.ASGITransport(app=app), drops=1, match=b"tools/call")

    def local_client(**kwargs):
        """Send the adapter's own client through the response-dropping transport."""
        return constructor(transport=dropper, **kwargs)

    monkeypatch.setattr(HTTP, "AsyncClient", local_client)

    async def check():
        async with app.router.lifespan_context(app):
            first = await call_remote(
                "https://memory.test/mcp",
                TOKEN,
                "qdrant_memory_add",
                {"text": "cats synthetic retry", "request_id": "retry-1"},
            )
            replay = await call_remote(
                "https://memory.test/mcp",
                TOKEN,
                "qdrant_memory_add",
                {"text": "cats synthetic retry", "request_id": "retry-1"},
            )
            return first, replay

    try:
        result, replay = asyncio.run(check())
        # One dropped tool response plus one successful replay after it.
        assert dropper.attempts == 3
        assert result["action"] == "ADD"
        assert replay == result
        assert rt.store.count() == 1
        assert rt.ledger.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
        assert rt.ledger.db.execute("SELECT COUNT(*) FROM shared_requests_v1").fetchone()[0] == 1
    finally:
        rt.ledger.close()
        rt.store.close()


def test_lost_event_response_replays_to_the_same_event(tmp_path, monkeypatch):
    """A turn committed before the cut keeps one event and one memory."""
    from qdrant_memory.owner_lifecycle import OwnerLifecycle
    from qdrant_memory.remote_gateway import create_gateway
    from qdrant_memory.shared_service import SharedOwner

    rt, device = _shared_owner(tmp_path)
    lifecycle = OwnerLifecycle(SharedOwner(rt, [device]))
    app = create_gateway(lifecycle.owner, allowed_hosts=["memory.test"], lifecycle=lifecycle)
    constructor = httpx.AsyncClient
    dropper = DropResponse(httpx.ASGITransport(app=app), drops=1)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: constructor(transport=dropper, **kwargs),
    )
    event = {
        "event_id": "turn-1",
        "session_id": "session-1",
        "kind": "turn_completed",
        "user": "cats synthetic lifecycle retry",
        "assistant": "ack",
    }

    async def check():
        async with app.router.lifespan_context(app):
            return await send_event("https://memory.test/mcp", TOKEN, event)

    try:
        result = asyncio.run(check())
        assert dropper.attempts == 2
        assert result["status"] == "COMMITTED"
        assert rt.ledger.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert rt.ledger.db.execute("SELECT COUNT(*) FROM owner_events_v1").fetchone()[0] == 1
        assert rt.store.count() == 1
    finally:
        rt.ledger.close()
        rt.store.close()


def test_rejection_is_not_replayed(tmp_path, monkeypatch):
    """A 401 never turns into a retry storm against a live service."""
    from qdrant_memory.remote_gateway import create_gateway
    from qdrant_memory.shared_service import SharedOwner

    rt, device = _shared_owner(tmp_path)
    app = create_gateway(SharedOwner(rt, [device]), allowed_hosts=["memory.test"])
    constructor = httpx.AsyncClient
    attempts = []

    def rejected(_request):
        """Answer every attempt with an authentication rejection."""
        attempts.append(1)
        return httpx.Response(401)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: constructor(transport=httpx.MockTransport(rejected), **kwargs),
    )

    async def check():
        async with app.router.lifespan_context(app):
            await send_event("https://memory.test/mcp", TOKEN, {"event_id": "turn-1"})

    try:
        with pytest.raises(Exception) as caught:
            asyncio.run(check())
        failure = remote_error(caught.value)
        assert failure["reason"] == "authentication_rejected"
        assert failure["retryable"] is False
        assert len(attempts) == 1
    finally:
        rt.ledger.close()
        rt.store.close()


def test_tool_path_authentication_rejection_is_not_replayed(tmp_path, monkeypatch):
    """A 401 from the real Gateway stays final instead of becoming a replay."""
    from qdrant_memory.remote_gateway import create_gateway
    from qdrant_memory.shared_service import SharedOwner

    rt, device = _shared_owner(tmp_path)
    app = create_gateway(SharedOwner(rt, [device]), allowed_hosts=["memory.test"])
    constructor = HTTP.AsyncClient
    counter = DropResponse(HTTP.ASGITransport(app=app), drops=0)
    monkeypatch.setattr(
        HTTP,
        "AsyncClient",
        lambda **kwargs: constructor(transport=counter, **kwargs),
    )

    async def check():
        async with app.router.lifespan_context(app):
            await call_remote(
                "https://memory.test/mcp",
                "b" * 40,
                "qdrant_memory_search",
                {"query": "cats"},
            )

    try:
        with pytest.raises(Exception) as caught:
            asyncio.run(check())
        failure = remote_error(caught.value)
        assert failure["reason"] == "authentication_rejected"
        assert failure["retryable"] is False
        assert counter.attempts == 1
    finally:
        rt.ledger.close()
        rt.store.close()


def test_lost_control_response_replays_one_cancellation(tmp_path, monkeypatch):
    """A lost cancel response converges on the same cancelled event."""
    from qdrant_memory.owner_lifecycle import OwnerLifecycle
    from qdrant_memory.remote_gateway import create_gateway
    from qdrant_memory.shared_service import SharedOwner

    rt, device = _shared_owner(tmp_path)
    lifecycle = OwnerLifecycle(SharedOwner(rt, [device]))
    monkeypatch.setattr(rt, "process_event", lambda _: None)
    app = create_gateway(lifecycle.owner, allowed_hosts=["memory.test"], lifecycle=lifecycle)
    constructor = httpx.AsyncClient
    dropper = DropResponse(httpx.ASGITransport(app=app), drops=1, match=b'"cancel"')
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: constructor(transport=dropper, **kwargs),
    )
    event = {
        "event_id": "turn-1",
        "session_id": "session-1",
        "kind": "turn_completed",
        "user": "cats synthetic cancel",
        "assistant": "ack",
    }

    async def check():
        async with app.router.lifespan_context(app):
            lifecycle.submit(TOKEN, event)
            return await control_event("https://memory.test/mcp", TOKEN, "turn-1", "cancel")

    try:
        result = asyncio.run(check())
        assert dropper.attempts == 2
        assert result["status"] == "CANCELLED"
        assert rt.ledger.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert rt.ledger.db.execute("SELECT status FROM events").fetchone()["status"] == "CANCELLED"
    finally:
        rt.ledger.close()
        rt.store.close()


def test_replay_budget_and_jitter_are_bounded(monkeypatch):
    """Three attempts with capped jitter, then the caller owns the retry."""
    import qdrant_memory.remote_client as module

    sleeps = []

    async def sleep(delay):
        """Record one backoff delay without waiting."""
        sleeps.append(delay)

    monkeypatch.setattr(module, "RETRY_SLEEP", sleep)
    attempts = []

    async def cut():
        """Fail with a replayable cut on every attempt."""
        attempts.append(1)
        raise httpx.RemoteProtocolError("synthetic")

    with pytest.raises(httpx.RemoteProtocolError):
        asyncio.run(module._replay(cut))
    assert len(attempts) == module.RETRY_ATTEMPTS
    assert len(sleeps) == module.RETRY_ATTEMPTS - 1
    for delay, attempt in zip(sleeps, range(len(sleeps))):
        assert 0 <= delay <= min(module.RETRY_CAP_SECONDS, module.RETRY_BASE_SECONDS * 2**attempt)


def test_slow_failure_is_left_to_the_durable_outbox(monkeypatch):
    """A 30-second timeout must not stack another 30-second attempt."""
    import qdrant_memory.remote_client as module

    sleeps, attempts, ticks = [], [], [0.0, 30.0]

    async def sleep(delay):
        """Record one backoff delay without waiting."""
        sleeps.append(delay)

    monkeypatch.setattr(module, "RETRY_SLEEP", sleep)
    monkeypatch.setattr(module, "RETRY_CLOCK", lambda: ticks.pop(0) if ticks else 30.0)

    async def slow_cut():
        """Fail after a timeout-sized wait."""
        attempts.append(1)
        raise httpx.ReadTimeout("synthetic")

    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(module._replay(slow_cut))
    assert len(attempts) == 1
    assert sleeps == []
    assert remote_error(httpx.ReadTimeout("synthetic"))["retryable"] is True


def test_rate_limit_window_is_left_to_the_outbox(monkeypatch):
    """A 60-second Retry-After is not replayed after 0.2 seconds."""
    import qdrant_memory.remote_client as module

    attempts = []

    async def sleep(_delay):
        """Skip the backoff delay in the test."""
        raise AssertionError("a long Retry-After must not sleep and retry")

    monkeypatch.setattr(module, "RETRY_SLEEP", sleep)
    constructor = httpx.AsyncClient

    def limited(_request):
        """Answer with the saturation window that the gateway sends."""
        attempts.append(1)
        return httpx.Response(429, headers={"Retry-After": "60"})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: constructor(transport=httpx.MockTransport(limited), **kwargs),
    )
    with pytest.raises(RemoteStatusError) as caught:
        asyncio.run(send_event("https://memory.test/mcp", TOKEN, {"event_id": "turn-1"}))
    assert len(attempts) == 1
    assert remote_error(caught.value)["reason"] == "temporarily_unavailable"
    assert remote_error(caught.value)["retryable"] is True


def test_tool_path_rate_limit_keeps_the_server_window(monkeypatch):
    """A saturation answer on the tool path is retryable and keeps its delay."""
    constructor = HTTP.AsyncClient
    attempts = []

    def limited(_request):
        """Answer with the saturation window that the gateway sends."""
        attempts.append(1)
        return HTTP.Response(429, headers={"Retry-After": "60"})

    monkeypatch.setattr(
        HTTP,
        "AsyncClient",
        lambda **kwargs: constructor(transport=HTTP.MockTransport(limited), **kwargs),
    )

    async def check():
        await call_remote(
            "https://memory.test/mcp",
            TOKEN,
            "qdrant_memory_search",
            {"query": "cats"},
        )

    with pytest.raises(Exception) as caught:
        asyncio.run(check())
    failure = remote_error(caught.value)
    assert failure["reason"] == "temporarily_unavailable"
    assert failure["retryable"] is True
    assert failure["retry_after"] == 60
    assert len(attempts) == 1


def test_control_error_stays_machine_readable_after_retries(monkeypatch):
    """A retryable control rejection is replayed, and a firm one is returned."""
    import qdrant_memory.remote_client as module

    bodies = [
        httpx.Response(503, json={"error": {"type": "OwnerEventError", "retryable": True}}),
        httpx.Response(400, json={"error": {"type": "KeyError", "retryable": False}}),
    ]
    attempts = []

    async def sleep(_delay):
        """Skip the backoff delay in the test."""
        return None

    monkeypatch.setattr(module, "RETRY_SLEEP", sleep)

    def handler(_request):
        """Serve the retryable rejection first, then the firm one."""
        attempts.append(1)
        return bodies[min(len(attempts), len(bodies)) - 1]

    constructor = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: constructor(transport=httpx.MockTransport(handler), **kwargs),
    )
    result = asyncio.run(control_event("https://memory.test/mcp", TOKEN, "turn-1", "status"))
    assert result["error"]["type"] == "KeyError"
    assert result["error"]["reason"] == "business_error"
    assert result["error"]["retryable"] is False
    assert "hint" in result["error"]
    assert "replay" not in result["error"]
    assert len(attempts) == 2


def test_exhausted_control_retry_names_the_event_to_replay(monkeypatch):
    """A retryable control failure tells the caller which event ID to resend."""
    import qdrant_memory.remote_client as module

    async def sleep(_delay):
        """Skip the backoff delay in the test."""
        return None

    monkeypatch.setattr(module, "RETRY_SLEEP", sleep)
    body = {"error": {"type": "OwnerEventError", "retryable": True}}
    constructor = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: constructor(
            transport=httpx.MockTransport(lambda _: httpx.Response(503, json=body)),
            **kwargs,
        ),
    )
    result = asyncio.run(control_event("https://memory.test/mcp", TOKEN, "turn-1", "status"))
    assert result["error"]["reason"] == "temporarily_unavailable"
    assert result["error"]["retryable"] is True
    assert result["error"]["replay"] == "event_id"
