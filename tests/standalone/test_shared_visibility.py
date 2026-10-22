"""Shared visibility must not leak, block, or latch across policy generations."""

from qdrant_memory.owner_lifecycle import OwnerLifecycle
from qdrant_memory.shared_service import SharedOwner

from ..helpers import grant, runtime


def test_cross_visibility_add_does_not_expose_or_conflict(tmp_path):
    """A reused space name must not turn an old visibility record into a 403 oracle."""
    rt = runtime(tmp_path)
    old_token, old = grant("old", visibility="private")
    new_token, new = grant("new", visibility="shared")
    SharedOwner(rt, [old]).call(
        old_token, "qdrant_memory_add", {"text": "Project Alpha", "request_id": "a1"}
    )

    owner = SharedOwner(rt, [new])
    result = owner.call(
        new_token, "qdrant_memory_add", {"text": "Project Alpha", "request_id": "b1"}
    )
    assert result["action"] == "ADD"
    memories = owner.call(new_token, "qdrant_memory_search", {"query": "Project Alpha"})["memories"]
    assert [row["id"] for row in memories] == [result["id"]]
    assert all(row["visibility"] == "shared" for row in memories)
    # The previous generation's record is not returned to this caller.
    assert len(list(rt.store.scroll())) == 2


def test_lifecycle_collision_skips_instead_of_blocking_every_writer(tmp_path):
    """A stale cross-visibility lifecycle target must not latch the Owner interlock."""
    rt = runtime(tmp_path)
    first_token, first = grant("first", visibility="shared", logical="agent-a")
    second_token, second = grant("second", visibility="project", logical="agent-b")
    first_owner = SharedOwner(rt, [first])
    first_lifecycle = OwnerLifecycle(first_owner)
    first_lifecycle.submit(
        first_token,
        {
            "event_id": "e1",
            "session_id": "s1",
            "kind": "turn_completed",
            "user": "Alpha uses WAL",
            "assistant": "ack",
        },
    )
    second_owner = SharedOwner(rt, [second])
    second_lifecycle = OwnerLifecycle(second_owner)
    second_lifecycle.submit(
        second_token,
        {
            "event_id": "e2",
            "session_id": "s2",
            "kind": "turn_completed",
            "user": "Alpha uses WAL",
            "assistant": "ack",
        },
    )
    assert second_owner.blocked is False
    second_memories = second_owner.call(
        second_token, "qdrant_memory_search", {"query": "Alpha uses WAL"}
    )["memories"]
    assert len(second_memories) == 1
    assert second_memories[0]["visibility"] == "project"
    first_memories = first_owner.call(
        first_token, "qdrant_memory_search", {"query": "Alpha uses WAL"}
    )["memories"]
    assert len(first_memories) == 1
    assert first_memories[0]["visibility"] == "shared"

    other_token, other = grant("other", visibility="shared", space="elsewhere")
    other_owner = SharedOwner(rt, [other])
    assert (
        other_owner.call(
            other_token, "qdrant_memory_add", {"text": "unrelated", "request_id": "c1"}
        )["action"]
        == "ADD"
    )


def test_out_of_boundary_extraction_is_counted_as_skip(tmp_path, monkeypatch):
    """A stale model-selected target is observable and never counted as an update."""
    rt = runtime(tmp_path)
    old_token, old = grant("old", visibility="shared")
    old_owner = SharedOwner(rt, [old])
    original = old_owner.call(
        old_token,
        "qdrant_memory_add",
        {
            "text": "Alpha uses WAL",
            "request_id": "old-add",
        },
    )
    token, current = grant("current", visibility="project", logical="agent-b")
    owner = SharedOwner(rt, [current])
    monkeypatch.setattr(
        "qdrant_memory.runtime.decide", lambda *a, **kw: ("UPDATE", original["id"], {})
    )
    OwnerLifecycle(owner).submit(
        token,
        {
            "event_id": "stale-target",
            "session_id": "s",
            "kind": "turn_completed",
            "user": "Alpha uses WAL",
            "assistant": "ack",
        },
    )
    counts = rt.ledger.stats()["dedupe"]
    assert counts["owner_record_boundary_skip"] == 1
    assert counts.get("UPDATE", 0) == 0
    assert rt.decisions.get("UPDATE", 0) == 0
    assert counts["SKIP"] == 1
    assert owner.blocked is False
    assert owner.call(token, "qdrant_memory_search", {"query": "Alpha"})["memories"] == []
