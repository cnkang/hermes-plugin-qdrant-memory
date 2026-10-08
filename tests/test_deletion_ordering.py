"""Order scoped deletes against replayable events and retries."""

import pytest

from qdrant_memory.models import Scope, content_hash
from qdrant_memory.tools import dispatch
from tests.helpers import runtime


def _queue_old_turn(rt, scope, turn_number=1):
    event = {
        "kind": "turn",
        "source": "conversation",
        "session_id": "alice-session",
        "scope": scope.as_dict(),
        "user": "cats preferred",
        "assistant": "",
        "turn_number": turn_number,
    }
    key = rt.ledger.enqueue_event(event)
    with rt.ledger.db:
        rt.ledger.db.execute(
            "UPDATE events SET created_at=? WHERE event_id=?",
            ("2000-01-01T00:00:00Z", key),
        )
    return key


def _add_original_memory(rt, scope):
    result = rt.add("cats preferred", scope, session_id="alice-session")
    assert result["action"] == "ADD"
    return result["id"]


def test_delete_fence_compares_instants_not_timestamp_strings(tmp_path):
    """A post-delete event with fractional seconds must not be lexically fenced."""
    rt = runtime(tmp_path)
    scope = Scope("alice", "hermes")
    try:
        rt.ledger.enqueue_operation(
            "memory",
            "DELETE",
            {},
            source_id=rt.ledger.delete_fence_source(scope),
            source_version="2026-10-08T12:00:00Z",
        )

        assert not rt.ledger.is_delete_fenced("memory", scope, "2026-10-08T12:00:00.000001Z")
        assert rt.ledger.is_delete_fenced("memory", scope, "invalid-timestamp")
        assert rt.ledger.is_delete_fenced("memory", scope, None)
    finally:
        rt.store.close()
        rt.ledger.close()


def test_unprepared_pre_delete_event_cannot_recreate_memory(tmp_path):
    """A previously admitted event must not restore an explicitly deleted point."""
    rt = runtime(tmp_path)
    scope = Scope("alice", "hermes")
    try:
        identifier = _add_original_memory(rt, scope)
        event_key = _queue_old_turn(rt, scope)

        result = dispatch(rt, "qdrant_memory_delete", {"id": identifier}, scope, "alice-session")
        assert result == {"id": identifier, "action": "DELETE"}
        assert rt.store.get(identifier, scope) is None

        # The event was admitted before deletion but had not prepared operations yet.
        rt.process_event(event_key)
        assert rt.ledger.row("events", event_key)["status"] == "COMMITTED"
        assert rt.store.get(identifier, scope) is None
        assert rt.store.search("cats", scope) == []
        assert rt.store.count(scope) == 0
        assert rt.ledger.is_delete_fenced(
            identifier, scope, "2000-01-01T00:00:00Z", content_hash("cats preferred")
        )
        assert not rt.ledger.is_delete_fenced(
            identifier, Scope("bob", "hermes"), "2000-01-01T00:00:00Z"
        )

        # A newly admitted event remains an intentional way to add the memory again.
        new_event_key = _queue_old_turn(rt, scope, turn_number=2)
        with rt.ledger.db:
            rt.ledger.db.execute(
                "UPDATE events SET created_at=? WHERE event_id=?",
                ("2999-01-01T00:00:00Z", new_event_key),
            )
        rt.process_event(new_event_key)
        hits = rt.store.search("cats", scope)
        assert len(hits) == 1
        assert hits[0].payload["text"] == "cats preferred"
    finally:
        rt.store.close()
        rt.ledger.close()


def test_failed_delete_keeps_fence_through_retry(tmp_path, monkeypatch):
    """Keep the deletion fence active until a failed delete is replayed."""
    rt = runtime(tmp_path)
    scope = Scope("alice", "hermes")
    try:
        identifier = _add_original_memory(rt, scope)
        event_key = _queue_old_turn(rt, scope)
        original_delete = rt.store.delete

        def fail_delete(_identifiers):
            raise ValueError("simulated delete failure")

        monkeypatch.setattr(rt.store, "delete", fail_delete)
        with pytest.raises(ValueError, match="simulated delete failure"):
            dispatch(rt, "qdrant_memory_delete", {"id": identifier}, scope, "alice-session")
        delete_rows = rt.ledger.db.execute(
            """SELECT idempotency_key,status FROM operations
            WHERE point_id=? AND action='DELETE' AND source_id=?""",
            (identifier, rt.ledger.delete_fence_source(scope)),
        ).fetchall()
        assert len(delete_rows) == 1
        assert delete_rows[0]["status"] == "FAILED"
        assert rt.ledger.is_delete_fenced(
            identifier, scope, "2000-01-01T00:00:00Z", content_hash("cats preferred")
        )

        monkeypatch.setattr(rt.store, "delete", original_delete)
        rt.ledger.retry_failed()
        assert rt.recover() == 0
        assert rt.store.get(identifier, scope) is None
        assert rt.store.search("cats", scope) == []
        assert rt.ledger.row("events", event_key)["status"] == "COMMITTED"
    finally:
        rt.store.close()
        rt.ledger.close()
