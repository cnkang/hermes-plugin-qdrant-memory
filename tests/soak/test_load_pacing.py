"""Contracts for soak-side Owner request pacing and durable retries."""

import asyncio
from types import SimpleNamespace

import pytest

from qdrant_memory.remote_client import RemoteStatusError
from scripts.soak.load import SoakClient, probe_rate_limit


class _Journal:
    def __init__(self):
        self.rows = []

    def record(self, kind, **fields):
        self.rows.append((kind, fields))


def _client(*, clock, sleep, journal=None):
    return SoakClient(
        endpoint="https://memory.test/mcp",
        ca_file=None,
        tokens={"writer": "x" * 40},
        journal=journal or _Journal(),
        clock=clock,
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
        attempts.append(1)
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

    assert attempts == [1]
    assert sleeps == []
    assert outcome.attempts == 1
    assert outcome.error["retry_after"] == 60


def test_rate_limit_probe_bypasses_normal_load_pacing():
    """Keep the dedicated burst identity able to exercise the gateway limiter."""

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
    assert len(client.calls) == 3
    assert all(call["pace_requests"] is False for call in client.calls)
