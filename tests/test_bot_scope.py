"""Keep bot turns and unattributed transcripts outside personal memory authority."""

import json
from threading import Event

import pytest

from qdrant_memory.models import Scope

from .test_provider import provider


def remember_alice(p):
    """Establish a human scope and one ordinary durable memory."""
    p.on_turn_start(1, "", author_id="alice")
    p.sync_turn("cats preferred", "ack", turn_author={"id": "alice"})
    assert p.wait_idle()
    return p.store.search("cats", Scope("alice", "hermes"))[0]


@pytest.mark.parametrize("author_id", ["bot:peer", None])
def test_bot_cannot_recall_or_use_any_memory_tool(tmp_path, monkeypatch, author_id):
    """Bot admission must not inherit the previous human's cached or tool authority."""
    p = provider(tmp_path)
    try:
        memory = remember_alice(p)
        p.queue_prefetch("cats")
        assert p.wait_idle()
        assert "cats preferred" in p.prefetch("cats")
        p.on_turn_start(2, "", author_id=author_id, author_is_bot=True)
        assert p.prefetch("cats") == ""

        def denied_search(*args, **kwargs):
            """Reject service access while bot prefetch admission is disabled."""
            pytest.fail("bot queued personal recall")

        monkeypatch.setattr(p.store, "search", denied_search)
        p.queue_prefetch("cats")
        assert p.wait_idle()
        for tool, args in (
            ("search", {"query": "cats"}),
            ("add", {"text": "dogs preferred"}),
            ("update", {"id": str(memory.id), "text": "dogs preferred"}),
            ("delete", {"id": str(memory.id)}),
        ):
            result = json.loads(p.handle_tool_call("qdrant_memory_" + tool, args))
            assert result["error"]["type"] == "PermissionError"
        p.sync_turn("dogs preferred", "ack")
        p.on_memory_write(
            "replace", "user", "dogs preferred", {"previous_content": "cats preferred"}
        )
        assert p.wait_idle()
        assert (
            p.store.get(str(memory.id), Scope("alice", "hermes")).payload["text"]
            == "cats preferred"
        )
        assert p.store.count() == 1
    finally:
        p.shutdown()


def test_bot_transition_invalidates_inflight_prefetch(tmp_path, monkeypatch):
    """A pending human recall result cannot repopulate the cache during a bot turn."""
    p = provider(tmp_path)
    entered, release = Event(), Event()
    try:
        remember_alice(p)
        search = p.store.search

        def slow_search(*args, **kwargs):
            """Hold recall until the active author has changed."""
            entered.set()
            assert release.wait(5)
            return search(*args, **kwargs)

        monkeypatch.setattr(p.store, "search", slow_search)
        p.queue_prefetch("cats")
        assert entered.wait(5)
        p.on_turn_start(2, "", author_is_bot=True)
        release.set()
        assert p.wait_idle()
        assert "first" not in p._cache
        p.on_turn_start(3, "", author_id="alice")
        assert p.prefetch("cats") == ""
    finally:
        release.set()
        p.shutdown()


@pytest.mark.parametrize("human_first", [False, True])
@pytest.mark.parametrize("later_human", [False, True])
def test_bot_transcript_archives_without_supplementary_extraction(
    tmp_path, human_first, later_human
):
    """Bot history remains unattributed even after the active turn becomes human."""
    p = provider(tmp_path)
    try:
        if human_first:
            p.on_turn_start(1, "", author_id="alice")
        p.on_turn_start(2, "", author_is_bot=True)
        if later_human:
            p.on_turn_start(3, "", author_id="alice")
        messages = [{"role": "user", "content": "dogs preferred"}]
        p.on_session_end(messages)
        p.on_pre_compress(messages, require_checkpoint=True)
        assert p.wait_idle()
        assert p.store.count() == 0
        assert p.context.llm.calls == []
        rows = p.ledger.rows("events", "COMMITTED")
        assert len(rows) == 1
        event = json.loads(rows[0]["payload_json"])
        assert event["scope"] == {"user_id": "__mixed__", "agent_id": None}
        assert event["extract"] is False
        assert event["evidence"] == messages
    finally:
        p.shutdown()


def test_quarantine_survives_switch_restart_and_rewind_until_reset(tmp_path):
    """Resuming a bot transcript must not turn it into a single-human transcript."""
    p = provider(tmp_path)
    p.on_turn_start(1, "", author_is_bot=True)
    p.on_session_switch("second")
    p.on_session_switch("first", rewound=True)
    assert json.loads(p.handle_tool_call("qdrant_memory_search", {"query": "cats"}))["error"]
    p.shutdown()
    p = provider(tmp_path)
    try:
        p.on_turn_start(2, "", author_id="alice")
        messages = [{"role": "user", "content": "dogs preferred"}]
        p.on_session_end(messages)
        p.on_pre_compress(messages, require_checkpoint=True)
        assert p.wait_idle()
        assert p.store.count() == 0
        p.on_session_switch("first", reset=True, user_id="alice")
        p.on_session_end([{"role": "user", "content": "cats preferred"}])
        assert p.wait_idle()
        assert p.store.count(Scope("alice", "hermes")) == 1
    finally:
        p.shutdown()


@pytest.mark.parametrize("reason", ["compression", "branch"])
def test_continuation_inherits_bot_restrictions_and_durable_quarantine(tmp_path, reason):
    """A new ID carrying the same transcript must retain its authorization boundaries."""
    p = provider(tmp_path)
    remember_alice(p)
    p.on_turn_start(2, "", author_is_bot=True)
    p.on_session_switch("child", parent_session_id="first", reset=False, reason=reason)
    assert p._scopes["child"] == Scope("alice", "hermes")
    for tool, args in (
        ("search", {"query": "cats"}),
        ("add", {"text": "dogs preferred"}),
    ):
        result = json.loads(p.handle_tool_call("qdrant_memory_" + tool, args))
        assert result["error"]["type"] == "PermissionError"
    p.queue_prefetch("cats")
    assert p.wait_idle()
    assert p.prefetch("cats") == ""
    p.shutdown()
    p = provider(tmp_path)
    try:
        p.on_session_switch("child")
        assert (
            json.loads(p.handle_tool_call("qdrant_memory_search", {"query": "cats"}))["error"][
                "type"
            ]
            == "PermissionError"
        )
        messages = [{"role": "user", "content": "dogs preferred"}]
        for human_resumed in (False, True):
            if human_resumed:
                p.on_turn_start(3, "", author_id="alice")
            p.on_session_end(messages)
            p.on_pre_compress(messages, require_checkpoint=True)
            assert p.wait_idle()
        assert p.store.count() == 1
        checkpoints = [
            json.loads(row["payload_json"])
            for row in p.ledger.rows("events", "COMMITTED")
            if json.loads(row["payload_json"])["kind"] == "checkpoint"
        ]
        assert checkpoints
        assert all(event["extract"] is False for event in checkpoints)
        assert all(event["scope"]["user_id"] == "__mixed__" for event in checkpoints)
        p.on_session_switch("fresh", parent_session_id="child", reset=True, user_id="alice")
        p.on_session_end([{"role": "user", "content": "birds preferred"}])
        assert p.wait_idle()
        assert p.store.count(Scope("alice", "hermes")) == 2
    finally:
        p.shutdown()


def test_reset_during_bot_turn_does_not_restore_human_authority(tmp_path):
    """Reset clears transcript history without authorizing the active bot."""
    p = provider(tmp_path)
    try:
        remember_alice(p)
        p.on_turn_start(2, "", author_is_bot=True)
        p.on_session_switch("first", reset=True, user_id="alice")
        result = json.loads(p.handle_tool_call("qdrant_memory_search", {"query": "cats"}))
        assert result["error"]["type"] == "PermissionError"
        p.on_session_end([{"role": "user", "content": "dogs preferred"}])
        assert p.wait_idle()
        assert p.store.count() == 1
    finally:
        p.shutdown()


@pytest.mark.parametrize("metadata", [{"author_name": "unknown"}, {}])
def test_unattributed_shared_turn_does_not_inherit_human_scope(tmp_path, metadata):
    """Missing identity in an already attributed session fails closed."""
    p = provider(tmp_path)
    try:
        remember_alice(p)
        p.on_turn_start(2, "", **metadata)
        result = json.loads(p.handle_tool_call("qdrant_memory_search", {"query": "cats"}))
        assert result["error"]["type"] == "PermissionError"
    finally:
        p.shutdown()


def test_captured_human_turn_and_cli_profile_behavior_remain_valid(tmp_path):
    """Delayed attributed human writes and ordinary unattributed CLI turns stay usable."""
    p = provider(tmp_path)
    try:
        p.on_turn_start(1, "")
        p.sync_turn("cats preferred", "ack")
        assert p.wait_idle()
        assert p.store.count(p.default_scope) == 1
        p.on_turn_start(2, "", author_id="alice")
        p.on_turn_start(3, "", author_is_bot=True)
        p.sync_turn("dogs preferred", "ack", turn_author={"id": "alice", "is_bot": False})
        assert p.wait_idle()
        assert p.store.count(Scope("alice", "hermes")) == 1
        p.on_turn_start(4, "", author_id="alice")
        result = json.loads(p.handle_tool_call("qdrant_memory_search", {"query": "dogs"}))
        assert [m["text"] for m in result["memories"]] == ["dogs preferred"]
    finally:
        p.shutdown()
