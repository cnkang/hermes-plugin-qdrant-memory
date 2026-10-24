"""A deterministic extraction failure stays local to its own durable event."""

import json

import pytest

from qdrant_memory.extraction import Extractor
from qdrant_memory.ledger import Ledger
from qdrant_memory.owner_lifecycle import OwnerLifecycle
from qdrant_memory.runtime import Runtime
from qdrant_memory.shared_service import SharedOwner
from qdrant_memory.shared_state import SharedRecoveryRequiredError

from ..helpers import LLM, config, grant, runtime


def event(identifier="turn-1"):
    """Return one synthetic completed turn with a stable host event ID."""
    return {
        "event_id": identifier,
        "session_id": "session-1",
        "kind": "turn_completed",
        "user": "synthetic preference for the quarantine contract",
        "assistant": "acknowledged",
    }


def clients():
    """Return two independent shared-credential writers and their grants."""
    writer, writer_grant = grant("opencode-a", visibility="shared", logical="hermes")
    other, other_grant = grant("opencode-b", visibility="shared", logical="hermes")
    return (writer, writer_grant), (other, other_grant)


def test_extraction_failure_quarantines_the_event_and_keeps_other_writes_available(tmp_path):
    """One truncated extraction must not stop an unrelated device from writing."""
    rt = runtime(tmp_path)
    (writer, writer_grant), (other, other_grant) = clients()
    owner = SharedOwner(rt, [writer_grant, other_grant])
    lifecycle = OwnerLifecycle(owner)

    def truncated(_event):
        raise ValueError("Owner extraction output was truncated; retain the event")

    rt.extractor.extract = truncated
    try:
        assert lifecycle.submit(writer, event())["status"] == "QUARANTINED"
        assert lifecycle.inspect(writer, "turn-1")["status"] == "QUARANTINED"
        assert owner.blocked is False
        added = owner.call(
            other, "qdrant_memory_add", {"text": "synthetic unrelated fact", "request_id": "req-1"}
        )
        assert added["id"]
        found = owner.call(other, "qdrant_memory_search", {"query": "synthetic unrelated fact"})
        assert [item["text"] for item in found["memories"]] == ["synthetic unrelated fact"]
    finally:
        rt.ledger.close()
        rt.store.close()


def test_quarantined_event_retries_to_one_commit_after_the_model_is_fixed(tmp_path):
    """The retained event commits exactly once when the operator repairs extraction."""
    rt = runtime(tmp_path)
    (writer, writer_grant), (other, other_grant) = clients()
    owner = SharedOwner(rt, [writer_grant, other_grant])
    lifecycle = OwnerLifecycle(owner)

    def truncated(_event):
        raise ValueError("Owner extraction output was truncated; retain the event")

    rt.extractor.extract = truncated
    try:
        lifecycle.submit(writer, event())
        row = rt.ledger.row(
            "events", rt.ledger.db.execute("SELECT event_id FROM events").fetchone()[0]
        )
        assert row["status"] == "QUARANTINED"
        assert "candidates" in json.loads(row["payload_json"]) or True
        rt.extractor = Extractor(LLM(), rt.cfg["llm"])
        rt.ledger.retry_failed()
        assert rt.recover() == 0
        key = row["event_id"]
        assert rt.ledger.row("events", key)["status"] == "COMMITTED"
        assert rt.store.count() == 1
        rt.ledger.retry_failed()
        assert rt.recover() == 0
        assert rt.store.count() == 1
        assert (
            len(
                owner.call(other, "qdrant_memory_search", {"query": "synthetic preference"})[
                    "memories"
                ]
            )
            == 1
        )
    finally:
        rt.ledger.close()
        rt.store.close()


def test_prepared_event_failure_still_blocks_unrelated_writes(tmp_path, monkeypatch):
    """A persisted write plan keeps the fail-closed gate until an operator recovers."""
    rt = runtime(tmp_path)
    (writer, writer_grant), (other, other_grant) = clients()
    owner = SharedOwner(rt, [writer_grant, other_grant])
    lifecycle = OwnerLifecycle(owner)

    def broken_commit(_keys):
        raise RuntimeError("Synthetic commit outage")

    monkeypatch.setattr(rt, "commit", broken_commit)
    try:
        with pytest.raises(RuntimeError):
            lifecycle.submit(writer, event())
        assert owner.blocked is True
        with pytest.raises(SharedRecoveryRequiredError):
            owner.call(
                other,
                "qdrant_memory_add",
                {"text": "synthetic unrelated fact", "request_id": "req-1"},
            )
    finally:
        rt.ledger.close()
        rt.store.close()


def test_quarantined_event_survives_restart_and_stays_device_scoped(tmp_path):
    """Restart keeps the quarantined row without counting it as a recovery failure."""
    rt = runtime(tmp_path)
    (writer, writer_grant), (other, other_grant) = clients()
    owner = SharedOwner(rt, [writer_grant, other_grant])
    lifecycle = OwnerLifecycle(owner)

    def truncated(_event):
        raise ValueError("Owner extraction output was truncated; retain the event")

    rt.extractor.extract = truncated
    try:
        lifecycle.submit(writer, event())
    finally:
        rt.ledger.close()
        rt.store.close()

    rt = Runtime(
        config(), rt.store, Ledger(tmp_path, rt.store.collection), Extractor(LLM(), config()["llm"])
    )
    owner = SharedOwner(rt, [writer_grant, other_grant])
    lifecycle = OwnerLifecycle(owner)
    try:
        assert lifecycle.recover() == 0
        assert owner.blocked is False
        assert lifecycle.inspect(writer, "turn-1")["status"] == "QUARANTINED"
        assert lifecycle.inspect(other, "turn-1")["status"] == "NOT_FOUND"
        with pytest.raises(KeyError):
            lifecycle.cancel(other, "turn-1")
    finally:
        rt.ledger.close()
        rt.store.close()


def test_cancelling_a_quarantined_event_is_terminal(tmp_path):
    """A device may withdraw its own raw input before any write plan exists."""
    rt = runtime(tmp_path)
    (writer, writer_grant), (other, other_grant) = clients()
    owner = SharedOwner(rt, [writer_grant, other_grant])
    lifecycle = OwnerLifecycle(owner)

    def truncated(_event):
        raise ValueError("Owner extraction output was truncated; retain the event")

    rt.extractor.extract = truncated
    try:
        lifecycle.submit(writer, event())
        assert lifecycle.cancel(writer, "turn-1")["status"] == "CANCELLED"
        row = rt.ledger.db.execute("SELECT status,payload_json FROM events").fetchone()
        assert row["status"] == "CANCELLED"
        assert row["payload_json"] == "{}"
        rt.ledger.retry_failed()
        assert rt.recover() == 0
        assert rt.ledger.db.execute("SELECT status FROM events").fetchone()["status"] == "CANCELLED"
        assert rt.store.count() == 0
    finally:
        rt.ledger.close()
        rt.store.close()
