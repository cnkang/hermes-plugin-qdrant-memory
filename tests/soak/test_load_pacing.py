"""Contracts for soak-side Owner request pacing and durable retries."""

import asyncio
from types import SimpleNamespace

import pytest

from qdrant_memory.remote_client import RemoteStatusError
from scripts.soak.load import (
    SoakClient,
    Workload,
    probe_authorization,
    probe_cas,
    probe_rate_limit,
)


class _Journal:
    def __init__(self):
        self.entries = []

    def record(self, kind, **fields):
        self.entries.append((kind, fields))

    def rows(self):
        return [{"kind": kind, **fields} for kind, fields in self.entries]


def _client(*, clock, sleep, wall_clock=None, journal=None):
    return SoakClient(
        endpoint="https://memory.test/mcp",
        ca_file=None,
        tokens={"writer": "x" * 40},
        journal=journal or _Journal(),
        clock=clock,
        wall_clock=wall_clock or (lambda: 1_000.0),
        sleep=sleep,
    )


def test_pacing_reserves_three_tool_posts_per_principal():
    """Serialize tool session initialization requests within one principal budget."""
    now = [0.0]
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    client = _client(clock=lambda: now[0], sleep=sleep)

    async def run():
        await client._pace("writer", weight=SoakClient.TOOL_POST_WEIGHT)
        await client._pace("writer", weight=SoakClient.TOOL_POST_WEIGHT)
        await client._pace("other", weight=SoakClient.EVENT_POST_WEIGHT)

    asyncio.run(run())

    assert sleeps == [pytest.approx(3 * SoakClient.POST_INTERVAL_SECONDS)]
    assert client._next_request_at["writer"] == pytest.approx(
        2 * 3 * SoakClient.POST_INTERVAL_SECONDS
    )


@pytest.mark.parametrize("operation", ["tool", "event"])
def test_retry_after_is_not_shortened_to_fit_the_outbox_deadline(operation, monkeypatch):
    """Leave a long retry pending when the measured window cannot honor it."""
    import qdrant_memory.remote_client as remote_client

    now = [0.0]
    sleeps = []
    attempts = []

    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    async def rejected(*args, **kwargs):
        attempts.append(kwargs)
        raise RemoteStatusError(429, retryable=True, retry_after=60)

    monkeypatch.setattr(remote_client, "call_remote", rejected)
    monkeypatch.setattr(remote_client, "send_event", rejected)
    client = _client(clock=lambda: now[0], sleep=sleep)

    async def run():
        if operation == "tool":
            return await client.call(
                "writer",
                "add",
                {"text": "soak"},
                op="add",
                request_id="request-1",
                outbox=30,
                pace_requests=False,
            )
        return await client.submit_event(
            "writer",
            {"event_id": "event-1", "user": "soak"},
            outbox=30,
        )

    outcome = asyncio.run(run())

    assert len(attempts) == 1
    assert attempts[0]["retry"] is False
    assert sleeps == []
    assert outcome.attempts == 1
    assert outcome.replay_count == 0
    assert outcome.error["retry_after"] == 60
    op = next(fields for kind, fields in client.journal.entries if kind == "op")
    assert op["retry_not_before"] == pytest.approx(1_060.0)


@pytest.mark.parametrize("operation", ["tool", "event"])
def test_recovery_waits_for_saved_retry_after(operation, monkeypatch):
    """Outbox drain must not immediately repeat a throttled request."""
    import qdrant_memory.remote_client as remote_client

    now = [0.0]
    wall = [1_000.0]
    sleeps = []
    sent_at = []

    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay
        wall[0] += delay

    async def remote(*args, **kwargs):
        sent_at.append(wall[0])
        assert kwargs["retry"] is False
        if len(sent_at) == 1:
            raise RemoteStatusError(429, retryable=True, retry_after=60)
        if operation == "event":
            return {"status": "COMMITTED"}
        return {"id": "record-1"}

    monkeypatch.setattr(remote_client, "call_remote", remote)
    monkeypatch.setattr(remote_client, "send_event", remote)
    journal = _Journal()
    client = _client(
        clock=lambda: now[0],
        wall_clock=lambda: wall[0],
        sleep=sleep,
        journal=journal,
    )
    workload = Workload.__new__(Workload)

    async def run():
        if operation == "tool":
            first = await client.call(
                "writer",
                "add",
                {"text": "soak"},
                op="add",
                request_id="request-1",
                outbox=30,
                pace_requests=False,
            )
        else:
            first = await client.submit_event(
                "writer",
                {"event_id": "event-1", "user": "soak"},
                outbox=30,
            )
        pending = workload.pending_writes(journal.rows())
        recovery = await workload.flush_pending(client, journal)
        return first, pending, recovery

    first, pending, recovery = asyncio.run(run())

    assert first.replay_count == 0
    assert next(iter(pending.values()))["retry_not_before"] == pytest.approx(1_060.0)
    assert sent_at == pytest.approx([1_000.0, 1_060.0])
    assert sleeps == [pytest.approx(60.0)]
    assert recovery == {"attempted": 1, "recovered": 1, "failed": 0}
    assert workload.pending_writes(journal.rows()) == {}


def test_pacing_skips_a_request_when_its_slot_is_past_the_deadline(monkeypatch):
    """Never start a remote operation after its paced slot misses the window."""
    now = [0.0]
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    client = _client(clock=lambda: now[0], sleep=sleep)
    client.deadline = 5.0
    client._next_request_at["writer"] = 10.0
    calls = []

    async def remote(*args, **kwargs):
        calls.append((args, kwargs))
        return {"id": "unexpected"}

    import qdrant_memory.remote_client as remote_client

    monkeypatch.setattr(remote_client, "call_remote", remote)
    outcome = asyncio.run(
        client.call(
            "writer",
            "add",
            {"text": "soak"},
            op="add",
            request_id="request-late",
        )
    )

    assert calls == []
    assert sleeps == []
    assert outcome.attempts == 0
    assert outcome.error["reason"] == "deadline_elapsed"


@pytest.mark.parametrize("operation", ["tool", "event"])
def test_retry_deadline_keeps_the_last_remote_failure(operation, monkeypatch):
    """A skipped replay remains attributed to the last Owner response."""
    import qdrant_memory.remote_client as remote_client

    now = [0.0]
    calls = []

    async def sleep(delay):
        now[0] += delay

    async def rejected(*args, **kwargs):
        calls.append((now[0], kwargs))
        raise RemoteStatusError(429, retryable=True, retry_after=0.2)

    monkeypatch.setattr(remote_client, "call_remote", rejected)
    monkeypatch.setattr(remote_client, "send_event", rejected)
    client = _client(clock=lambda: now[0], sleep=sleep)

    async def run():
        if operation == "tool":
            return await client.call(
                "writer",
                "add",
                {"text": "soak"},
                op="add",
                request_id="request-deadline",
                outbox=0.3,
            )
        return await client.submit_event(
            "writer",
            {"event_id": "event-deadline", "user": "soak"},
            outbox=0.3,
        )

    outcome = asyncio.run(run())

    assert len(calls) == 1
    assert outcome.attempts == 1
    assert outcome.replay_count == 0
    assert outcome.error["status_code"] == 429
    assert outcome.error["retry_after"] == pytest.approx(0.2)


def test_explicit_outbox_deadline_is_not_shortened_by_the_run_deadline(monkeypatch):
    """Allow explicit outbox work to finish within its own deadline."""
    now = [0.0]
    sleeps = []
    calls = []

    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    async def remote(*args, **kwargs):
        calls.append(now[0])
        return {"id": "record-1"}

    import qdrant_memory.remote_client as remote_client

    monkeypatch.setattr(remote_client, "call_remote", remote)
    client = _client(clock=lambda: now[0], sleep=sleep)
    client.deadline = 0.5
    client._next_request_at["writer"] = 1.0

    outcome = asyncio.run(
        client.call(
            "writer",
            "add",
            {"text": "soak"},
            op="add",
            request_id="request-outbox",
            outbox=2.0,
        )
    )

    assert outcome.ok is True
    assert calls == [pytest.approx(1.0)]
    assert sleeps == [pytest.approx(1.0)]


def test_owner_sessions_are_limited_to_four_in_flight_per_identity(monkeypatch):
    """Keep the Owner's per-principal admission slots below its hard limit."""
    import qdrant_memory.remote_client as remote_client

    active = 0
    peak = 0

    async def remote(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"id": "record"}

    monkeypatch.setattr(remote_client, "call_remote", remote)
    client = _client(clock=lambda: 0.0, sleep=asyncio.sleep)

    async def run():
        return await asyncio.gather(
            *(
                client.call(
                    "writer",
                    "add",
                    {"text": "soak"},
                    op="add",
                    request_id=f"request-{index}",
                    pace_requests=False,
                )
                for index in range(10)
            )
        )

    outcomes = asyncio.run(run())

    assert all(outcome.ok for outcome in outcomes)
    assert peak == 4


def test_lost_response_probe_is_deferred_when_pacing_deadline_has_passed():
    """Do not send the direct network probe after its run window expires."""
    client = _client(clock=lambda: 10.0, sleep=asyncio.sleep)
    client.deadline = 10.0

    result = asyncio.run(
        client.abandon_response("writer", "add", {"text": "soak"}, request_id="lost-1")
    )

    assert result["deferred"] is True
    assert result["status"] is None
    kind, fields = client.journal.entries[-1]
    assert kind == "lost_response"
    assert fields["deferred"] is True


def test_remote_client_can_disable_internal_replay(monkeypatch):
    """Expose one transport attempt so the soak layer can pace every replay."""
    import mcp
    import mcp.client.streamable_http as streamable_http

    import qdrant_memory.remote_client as remote_client

    class _AsyncContext:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *args):
            return None

    class _HttpClient(_AsyncContext):
        def __init__(self, **kwargs):
            super().__init__(self)

    class _Session(_AsyncContext):
        def __init__(self, receive, send):
            super().__init__(self)
            self.initialized = False
            self.called = False

        async def initialize(self):
            self.initialized = True

        async def call_tool(self, name, arguments):
            self.called = True
            return object()

    session = _Session(None, None)
    monkeypatch.setattr(mcp, "ClientSession", lambda *args: session)
    monkeypatch.setattr(
        streamable_http,
        "streamable_http_client",
        lambda *args, **kwargs: _AsyncContext((None, None)),
        raising=False,
    )
    monkeypatch.setattr(
        remote_client, "http_module", lambda: SimpleNamespace(AsyncClient=_HttpClient)
    )
    monkeypatch.setattr(remote_client, "validate_endpoint", lambda url: None)
    monkeypatch.setattr(remote_client, "validate_arguments", lambda *args: None)
    monkeypatch.setattr(remote_client, "_tool_value", lambda value: {"ok": True})

    async def unexpected_replay(*args, **kwargs):
        pytest.fail("retry=False must not enter the internal replay loop")

    monkeypatch.setattr(remote_client, "_replay", unexpected_replay)

    result = asyncio.run(
        remote_client.call_remote(
            "https://memory.test/mcp",
            "x" * 40,
            "qdrant_memory_search",
            {},
            retry=False,
        )
    )

    assert result == {"ok": True}
    assert session.initialized is True
    assert session.called is True

    event_attempts = []

    async def reject_event(*args, **kwargs):
        event_attempts.append(kwargs)
        raise RemoteStatusError(429, retryable=True, retry_after=60)

    monkeypatch.setattr(remote_client, "_event_request", reject_event)

    with pytest.raises(RemoteStatusError):
        asyncio.run(
            remote_client.send_event(
                "https://memory.test/mcp",
                "x" * 40,
                {"event_id": "event-1"},
                retry=False,
            )
        )

    assert len(event_attempts) == 1


def test_rate_limit_probe_bypasses_normal_load_pacing():
    """Keep the dedicated burst probe unpaced and stop at its first backoff."""

    class _Client:
        def __init__(self):
            self.calls = []

        async def call(self, *args, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                ok=False,
                error={"reason": "temporarily_unavailable", "retryable": True, "retry_after": 60},
            )

    client = _Client()
    journal = _Journal()

    result = asyncio.run(probe_rate_limit(client, journal))

    assert result["status"] == "PASS"
    assert len(client.calls) == 1
    assert all(call["pace_requests"] is False for call in client.calls)


@pytest.mark.parametrize("operation", ["tool", "event"])
def test_recovery_required_is_returned_before_an_explicit_same_id_retry(operation, monkeypatch):
    """The caller sees recovery_required; only a later user retry reuses its ID."""
    import qdrant_memory.remote_client as remote_client

    requests = []
    generation = [0]
    recovery_requests = []

    async def request_owner_recovery(previous_generation, identity, request_id):
        """Capture one recovery trigger without restarting an Owner in the test."""
        recovery_requests.append((previous_generation, identity, request_id))
        return True

    async def request(*args, **kwargs):
        """Return a recovery gate once, then acknowledge the original request."""
        requests.append((args, kwargs))
        if len(requests) == 1:
            raise RemoteStatusError(
                503,
                error_type="SharedRecoveryRequiredError",
                retryable=False,
            )
        return (
            {"action": "ADD", "id": "record-1"} if operation == "tool" else {"status": "COMMITTED"}
        )

    monkeypatch.setattr(remote_client, "call_remote", request)
    monkeypatch.setattr(remote_client, "send_event", request)
    now = [0.0]

    async def sleep(_delay):
        """Skip backoff because the mocked request resolves immediately."""
        pass

    journal = _Journal()
    client = _client(clock=lambda: now[0], sleep=sleep, journal=journal)
    client.owner_recovery_generation = lambda: generation[0]
    client.owner_recovery_handler = request_owner_recovery

    async def submit():
        """Submit either the tool or event used by this parametrized test."""
        if operation == "tool":
            return await client.call(
                "writer",
                "add",
                {"text": "recover me"},
                op="add",
                request_id="request-1",
                pace_requests=False,
            )
        return await client.submit_event(
            "writer",
            {"event_id": "event-1", "user": "recover me"},
        )

    failed = asyncio.run(submit())

    assert not failed.ok
    assert failed.error["reason"] == "recovery_required"
    assert failed.error["retryable"] is False
    assert not failed.resolved
    assert failed.attempts == 1
    assert failed.replay_count == 0
    assert len(requests) == 1
    assert recovery_requests == [(0, "writer", "request-1" if operation == "tool" else "event-1")]
    first = next(fields for kind, fields in journal.entries if kind == "op")
    assert first["result"] == "error"
    assert first["error"]["retryable"] is False
    assert first["owner_recovery_generation"] == 0
    assert first["operator_recovery_requested"] is True

    # Simulate the separately logged operator restart completing. The caller
    # explicitly submits its saved operation again under the original ID.
    generation[0] = 1
    recovered = asyncio.run(submit())

    assert recovered.ok
    assert recovered.attempts == 1
    assert recovered.replay_count == 0
    assert len(requests) == 2
    if operation == "tool":
        assert requests[0][0][3]["request_id"] == requests[1][0][3]["request_id"] == "request-1"
    else:
        assert requests[0][0][2]["event_id"] == requests[1][0][2]["event_id"] == "event-1"


def test_pending_writes_include_only_recovery_gated_requests_queued_for_operator_replay():
    """A non-retryable Owner gate enters the drain only after recovery was queued."""
    from scripts.soak.load import Workload

    workload = Workload.__new__(Workload)
    row = {
        "kind": "op",
        "op": "add",
        "tool": "add",
        "request_id": "write-1",
        "result": "error",
        "error": {"reason": "recovery_required", "retryable": False},
        "operator_recovery_requested": True,
    }

    assert workload.pending_writes([row]) == {"write-1": row}
    row["operator_recovery_requested"] = False
    assert workload.pending_writes([row]) == {}


def test_nonretryable_lifecycle_failure_is_resolved_as_terminal():
    """Lifecycle and tool outcomes share the terminal-failure classification."""
    now = [0.0]

    async def sleep(_delay):
        """Skip backoff for the synthetic lifecycle rejection."""
        pass

    client = _client(clock=lambda: now[0], sleep=sleep)

    async def rejected(_identity, _request, *, deadline):
        """Return one permanent permission-style rejection."""
        return (
            None,
            {"type": "OwnerError", "reason": "forbidden", "retryable": False},
            0.01,
            True,
            0.0,
        )

    client._guarded_attempt = rejected
    outcome = asyncio.run(
        client.submit_event("writer", {"event_id": "event-1", "user": "not authorized"})
    )

    assert not outcome.ok
    assert outcome.error["reason"] == "forbidden"
    assert outcome.resolved


def test_authorization_probe_retries_same_marker_after_owner_recovery():
    """A fail-closed recovery gate defers the probe without reporting a false failure."""
    journal = _Journal()
    responses = [
        SimpleNamespace(
            ok=False,
            value={},
            error={
                "type": "SharedRecoveryRequiredError",
                "reason": "business_error",
                "retryable": False,
            },
        ),
        SimpleNamespace(ok=True, value={"id": "private-1"}, error={}),
        SimpleNamespace(ok=False, value={}, error={"type": "PermissionError"}),
        SimpleNamespace(ok=True, value={"memories": []}, error={}),
        SimpleNamespace(ok=True, value={"memories": []}, error={}),
    ]
    calls = []

    class Client:
        async def call(self, identity, tool, arguments, **kwargs):
            """Return the next scripted authorization outcome."""
            calls.append((identity, tool, kwargs.get("request_id")))
            return responses.pop(0)

    client = Client()
    first = asyncio.run(probe_authorization(client, journal))
    assert first["status"] == "DEFERRED"
    assert journal.entries[0][0] == "probe_deferred"

    second = asyncio.run(probe_authorization(client, journal, marker=first["marker"]))

    assert second["status"] == "PASS"
    assert calls[0][2] == calls[1][2]
    assert [kind for kind, _ in journal.entries] == ["probe_deferred", "probe"]


def test_cas_probe_retries_same_request_ids_after_owner_recovery():
    """An interrupted CAS probe resumes its original add and competing writes."""
    journal = _Journal()
    blocked = SimpleNamespace(
        ok=False,
        value={},
        error={"type": "SharedRecoveryRequiredError", "reason": "recovery_required"},
    )
    added = SimpleNamespace(ok=True, value={"id": "record-1", "version": "v1"}, error={})
    updated = SimpleNamespace(
        ok=True,
        value={"id": "record-1", "version": "v2", "action": "UPDATE"},
        error={},
        action="UPDATE",
    )
    conflict = SimpleNamespace(
        ok=False,
        value={},
        error={"type": "SharedConflictError", "reason": "conflict"},
    )
    responses = [added, blocked, updated, added, conflict, updated]
    calls = []

    class Client:
        async def call(self, identity, tool, arguments, **kwargs):
            """Return the next scripted CAS outcome."""
            calls.append((identity, tool, kwargs["request_id"]))
            return responses.pop(0)

    client = Client()
    first = asyncio.run(probe_cas(client, journal))
    assert first["status"] == "DEFERRED"

    second = asyncio.run(probe_cas(client, journal, marker=first["marker"]))

    assert second["status"] == "PASS"
    assert calls[:3] == calls[3:]
    assert [kind for kind, _ in journal.entries] == ["probe_deferred", "probe"]
