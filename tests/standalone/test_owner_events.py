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


def test_cancellation_detects_a_changed_ledger_plan(tmp_path, monkeypatch):
    """A plan prepared between validation and update must never be discarded."""
    import qdrant_memory.owner_lifecycle as module

    rt = runtime(tmp_path)
    token = "a" * 40
    lifecycle = OwnerLifecycle(SharedOwner(rt, [grant(token, "mac")]))
    try:
        monkeypatch.setattr(rt, "process_event", lambda _: None)
        lifecycle.submit(token, event())
        row = rt.ledger.db.execute("SELECT * FROM events").fetchone()
        loads = json.loads
        changed = False

        def prepare_during_validation(raw):
            nonlocal changed
            value = loads(raw)
            if not changed:
                changed = True
                rt.ledger.prepare_event(row["event_id"], {**value, "candidates": []})
            return value

        monkeypatch.setattr(module.json, "loads", prepare_during_validation)
        with pytest.raises(ValueError, match="prepared|committed"):
            lifecycle.cancel(token, "one")
        assert rt.ledger.row("events", row["event_id"])["status"] == "PENDING"
    finally:
        rt.ledger.close()
        rt.store.close()
