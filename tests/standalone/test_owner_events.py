"""Credential-bound inspection/cancellation never abandons prepared mutations."""

import json

import pytest

from qdrant_memory.owner_lifecycle import OwnerLifecycle
from qdrant_memory.shared_service import SharedOwner

from ..helpers import runtime
from .test_device_grants import grant


def event(identifier="one"):
    """Return synthetic permitted host content with a stable event ID."""
    return {
        "event_id": identifier,
        "session_id": "s",
        "kind": "turn_completed",
        "user": "cats synthetic private",
        "assistant": "ack",
    }


def test_cancel_pending_is_durable_idempotent_and_scrubs_raw_content(tmp_path, monkeypatch):
    """Cancellation is terminal, surviving replay and recovery without a database write."""
    rt = runtime(tmp_path)
    token = "a" * 40
    owner = SharedOwner(rt, [grant(token, "mac")])
    lifecycle = OwnerLifecycle(owner)
    try:
        monkeypatch.setattr(rt, "process_event", lambda _: None)
        assert lifecycle.submit(token, event())["status"] == "PENDING"
        assert lifecycle.inspect(token, "one")["status"] == "PENDING"
        assert lifecycle.cancel(token, "one")["status"] == "CANCELLED"
        assert lifecycle.cancel(token, "one")["status"] == "CANCELLED"
        assert lifecycle.submit(token, event())["status"] == "CANCELLED"
        assert lifecycle.recover() == 0
        row = rt.ledger.db.execute("SELECT status,payload_json FROM events").fetchone()
        assert row["status"] == "CANCELLED"
        assert row["payload_json"] == "{}"
        plan = rt.ledger.db.execute("SELECT event_json FROM owner_events_v1").fetchone()
        assert plan["event_json"] == "{}"
        from qdrant_memory.ledger import Ledger
        from qdrant_memory.runtime import Runtime

        rt.ledger.close()
        rt = Runtime(rt.cfg, rt.store, Ledger(tmp_path, rt.store.collection), rt.extractor)
        lifecycle = OwnerLifecycle(SharedOwner(rt, [grant(token, "mac")]))
        assert lifecycle.recover() == 0
        assert lifecycle.submit(token, event())["status"] == "CANCELLED"
        assert rt.store.count() == 0
    finally:
        rt.ledger.close()
        rt.store.close()


def test_event_controls_cannot_cross_device_or_revoked_authority(tmp_path, monkeypatch):
    """Even a second device sharing private recall cannot cancel another device's event."""
    rt = runtime(tmp_path)
    a, b = "a" * 40, "b" * 40
    clients = [grant(a, "mac"), grant(b, "linux")]
    lifecycle = OwnerLifecycle(SharedOwner(rt, clients))
    try:
        monkeypatch.setattr(rt, "process_event", lambda _: None)
        lifecycle.submit(a, event())
        assert lifecycle.inspect(b, "one")["status"] == "NOT_FOUND"
        with pytest.raises(KeyError):
            lifecycle.cancel(b, "one")
        clients[0]["revoked"] = True
        with pytest.raises(PermissionError):
            lifecycle.inspect(a, "one")
        with pytest.raises(PermissionError):
            lifecycle.cancel(a, "one")
    finally:
        rt.ledger.close()
        rt.store.close()


def test_cancellation_never_reverses_committed_or_prepared_operations(tmp_path, monkeypatch):
    """A possibly accepted write must reconcile through the existing Runtime."""
    rt = runtime(tmp_path)
    token = "a" * 40
    lifecycle = OwnerLifecycle(SharedOwner(rt, [grant(token, "mac")]))
    try:
        lifecycle.submit(token, event())
        with pytest.raises(ValueError, match="prepared|committed"):
            lifecycle.cancel(token, "one")

        def fail(_):
            raise TimeoutError("synthetic lost acknowledgement")

        monkeypatch.setattr(rt, "commit", fail)
        with pytest.raises(TimeoutError):
            lifecycle.submit(token, {**event("two"), "user": "dogs separate"})
        with pytest.raises(ValueError, match="prepared|committed"):
            lifecycle.cancel(token, "two")
        row = rt.ledger.db.execute(
            "SELECT payload_json FROM events WHERE status='FAILED'"
        ).fetchone()
        assert json.loads(row["payload_json"])["operation_keys"]
    finally:
        rt.ledger.close()
        rt.store.close()


def test_cancel_failed_extraction_keeps_existing_owner_recovery_gate(tmp_path, monkeypatch):
    """Cancelling raw input is not permission to clear an existing recovery gate."""
    rt = runtime(tmp_path)
    token = "a" * 40
    owner = SharedOwner(rt, [grant(token, "mac")])
    lifecycle = OwnerLifecycle(owner)

    def fail(_):
        raise TimeoutError("synthetic extraction outage")

    try:
        monkeypatch.setattr(rt.extractor, "extract", fail)
        with pytest.raises(TimeoutError):
            lifecycle.submit(token, event())
        assert lifecycle.cancel(token, "one")["status"] == "CANCELLED"
        assert owner.blocked is True
    finally:
        rt.ledger.close()
        rt.store.close()


def test_event_management_http_reuses_auth_and_rejects_scope_parameters(tmp_path, monkeypatch):
    """Event controls are opt-in Owner endpoints, never new MCP memory tools."""
    import asyncio

    import httpx

    from qdrant_memory.remote_gateway import create_gateway

    rt = runtime(tmp_path)
    token = "a" * 40
    lifecycle = OwnerLifecycle(SharedOwner(rt, [grant(token, "mac")]))
    monkeypatch.setattr(rt, "process_event", lambda _: None)
    app = create_gateway(lifecycle.owner, allowed_hosts=["memory.test"], lifecycle=lifecycle)

    async def check():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://memory.test"
            ) as http:
                headers = {"Authorization": "Bearer " + token}
                await http.post("/events", headers=headers, json=event())
                body = {"action": "status", "event_id": "one"}
                assert (await http.post("/events/manage", headers=headers, json=body)).json()[
                    "status"
                ] == "PENDING"
                assert (await http.post("/events/manage", json=body)).status_code == 403
                assert (
                    await http.post(
                        "/events/manage", headers=headers, json={**body, "owner_id": "forged"}
                    )
                ).status_code == 400
                body["action"] = "cancel"
                assert (await http.post("/events/manage", headers=headers, json=body)).json()[
                    "status"
                ] == "CANCELLED"

    try:
        asyncio.run(check())
    finally:
        rt.ledger.close()
        rt.store.close()


def test_production_event_client_control_and_cancelled_replay(tmp_path, monkeypatch):
    """The CLI transport receives cancellation as a terminal result, not a write ack."""
    import asyncio

    import httpx

    from qdrant_memory.remote_client import control_event, send_event
    from qdrant_memory.remote_gateway import create_gateway

    rt = runtime(tmp_path)
    token = "a" * 40
    lifecycle = OwnerLifecycle(SharedOwner(rt, [grant(token, "mac")]))
    monkeypatch.setattr(rt, "process_event", lambda _: None)
    app = create_gateway(lifecycle.owner, allowed_hosts=["memory.test"], lifecycle=lifecycle)
    constructor = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: constructor(transport=httpx.ASGITransport(app=app), **kwargs),
    )

    async def check():
        async with app.router.lifespan_context(app):
            with pytest.raises(RuntimeError, match="not committed"):
                await send_event("https://memory.test/mcp", token, event())
            assert (await control_event("https://memory.test/mcp", token, "one", "status"))[
                "status"
            ] == "PENDING"
            assert (await control_event("https://memory.test/mcp", token, "one", "cancel"))[
                "status"
            ] == "CANCELLED"
            assert (await send_event("https://memory.test/mcp", token, event()))[
                "status"
            ] == "CANCELLED"

    try:
        asyncio.run(check())
    finally:
        rt.ledger.close()
        rt.store.close()


def test_public_event_control_cli_requires_only_event_id(monkeypatch, capsys):
    """No command-line payload can override trusted Owner identity or space."""
    import io
    import sys

    from qdrant_memory.standalone import main

    seen = []

    async def control(url, token, event_id, action, **kwargs):
        seen.append((event_id, action))
        return {"event_id": event_id, "status": "CANCELLED"}

    monkeypatch.setattr("qdrant_memory.remote_client.control_event", control)
    monkeypatch.setenv("QM_EVENT_CONTROL", "a" * 40)
    args = [
        "remote-event",
        "--url",
        "https://memory.test/mcp",
        "--token-env",
        "QM_EVENT_CONTROL",
        "--action",
        "cancel",
    ]
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"event_id":"one"}')))
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "CANCELLED"
    assert seen == [("one", "cancel")]
    monkeypatch.setattr(
        sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"event_id":"one","space_id":"spoof"}'))
    )
    assert main(args) == 1
    assert seen == [("one", "cancel")]
