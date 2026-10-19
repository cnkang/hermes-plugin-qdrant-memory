"""Owner lifecycle reuses extraction, durable events, dedupe and the mutation engine."""

import pytest

from qdrant_memory.owner_lifecycle import OwnerLifecycle
from qdrant_memory.shared_service import SharedOwner

from ..helpers import runtime
from .test_device_grants import grant


def test_private_lifecycle_idempotency_provenance_and_shared_rule(tmp_path):
    """A completed permitted turn is extracted centrally and returned through Owner tools."""
    token = "a" * 40
    credential = grant(token, "mac")
    rt = runtime(tmp_path)
    try:
        owner = SharedOwner(rt, [credential])
        lifecycle = OwnerLifecycle(owner)
        event = {
            "event_id": "turn-one",
            "session_id": "session",
            "kind": "turn_completed",
            "user": "cats private",
            "assistant": "ack",
        }
        result = lifecycle.submit(token, event)
        assert result["status"] == "COMMITTED"
        assert lifecycle.submit(token, event) == result
        assert len(rt.extractor.llm.calls) == 1
        rows = owner.call(token, "qdrant_memory_search", {"query": "cats"})["memories"]
        assert len(rows) == 1 and rows[0]["created_by_device"] == "mac"
        with pytest.raises(ValueError):
            lifecycle.submit(token, {**event, "user": "other"})
        with pytest.raises(ValueError):
            lifecycle.submit(token, {**event, "principal": "spoof"})
        credential["visibility"] = "project"
        with pytest.raises(PermissionError):
            lifecycle.submit(token, {**event, "event_id": "project"})
        credential["capture_rule_id"] = "user-approved-alpha"
        assert lifecycle.submit(token, {**event, "event_id": "project"})["status"] == "COMMITTED"
    finally:
        rt.ledger.close()
        rt.store.close()


def test_lifecycle_outage_is_durable_and_recoverable(tmp_path, monkeypatch):
    """The accepted event survives extraction failure without a second runtime."""
    token = "a" * 40
    rt = runtime(tmp_path)
    try:
        owner = SharedOwner(rt, [grant(token, "mac")])
        lifecycle = OwnerLifecycle(owner)
        extract = rt.extractor.extract

        def fail(_):
            raise TimeoutError("synthetic-private-error")

        monkeypatch.setattr(rt.extractor, "extract", fail)
        event = {
            "event_id": "outage",
            "session_id": "s",
            "kind": "turn_completed",
            "user": "cats durable",
            "assistant": "ack",
        }
        with pytest.raises(TimeoutError):
            lifecycle.submit(token, event)
        assert rt.ledger.stats()["events"]["FAILED"] == 1
        monkeypatch.setattr(rt.extractor, "extract", extract)
        rt.ledger.retry_failed()
        assert rt.recover() == 0
        assert lifecycle.submit(token, event)["status"] == "COMMITTED"
        assert len(owner.call(token, "qdrant_memory_search", {"query": "cats"})["memories"]) == 1
    finally:
        rt.ledger.close()
        rt.store.close()
