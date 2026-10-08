"""Exercise checkpoint and session lifecycle through the actual Hermes manager."""

import json
import sqlite3
from types import SimpleNamespace

import pytest
from agent.context_compressor import COMPRESSED_SUMMARY_METADATA_KEY
from agent.conversation_compression import (
    CompressionCheckpointUnavailable,
    _pre_compress_memory_context,
)
from agent.memory_manager import MemoryManager

from .helpers import capture_committed_event_payloads
from .test_provider import provider


def test_checkpoint_commits_normalized_evidence_and_propagates_failure(tmp_path, monkeypatch):
    """Host compression sees a committed checkpoint and blocks on disk failure."""
    p = provider(tmp_path)
    manager = MemoryManager()
    manager.add_provider(p)
    agent = SimpleNamespace(_memory_manager=manager)
    messages = [
        {"role": "user", "content": "cats preferred"},
        {"role": "assistant", "content": "ack", "tool_calls": [{"id": "tool"}]},
        {"role": "tool", "content": "secret tool output"},
        {
            "role": "assistant",
            "content": "generated summary",
            COMPRESSED_SUMMARY_METADATA_KEY: True,
        },
    ]
    try:
        assert manager.supports_pre_compress_checkpoint()
        _pre_compress_memory_context(agent, messages, True)
        with sqlite3.connect(tmp_path / "qdrant-memory/state.db") as db:
            event = json.loads(db.execute("SELECT payload_json FROM events").fetchone()[0])
        assert event["evidence"] == [
            {"role": "user", "content": "cats preferred"},
            {"role": "assistant", "content": "ack"},
        ]
        assert p.wait_idle()

        def disk_failure(value):
            """Simulate a failed durable commit before a lossy rewrite."""
            raise sqlite3.OperationalError("disk unavailable")

        monkeypatch.setattr(p.ledger, "enqueue_event", disk_failure)
        with pytest.raises(CompressionCheckpointUnavailable):
            _pre_compress_memory_context(agent, messages, True)
    finally:
        p.shutdown()


def test_mixed_author_checkpoint_archives_without_attributing_facts(tmp_path):
    """Mixed-author evidence remains durable without becoming one user's memory."""
    p = provider(tmp_path)
    try:
        p.on_turn_start(1, "", author_id="alice")
        p.on_turn_start(2, "", author_id="bob")
        p.on_pre_compress([{"role": "user", "content": "cats preferred"}], require_checkpoint=True)
        assert p.wait_idle()
        assert p.store.count() == 0
        assert p.ledger.stats()["events"]["COMMITTED"] == 1
    finally:
        p.shutdown()


def test_mixed_author_checkpoint_stores_neutral_scope(tmp_path, monkeypatch):
    """Multi-author checkpoint events store a non-attributed '__mixed__' scope."""
    p = provider(tmp_path)
    captured = capture_committed_event_payloads(monkeypatch, p.ledger)
    try:
        _assert_neutral_scope_in_mixed_author_checkpoint(p, tmp_path)
        event = captured[-1]
        assert event["scope"]["user_id"] == "__mixed__"
        assert event["scope"]["agent_id"] is None
    finally:
        p.shutdown()


def _assert_neutral_scope_in_mixed_author_checkpoint(p, tmp_path):
    """Assert a multi-author checkpoint persists a '__mixed__' neutral scope.

    Args:
        p: Provider instance with an in-memory Qdrant backend.
        tmp_path: Temporary directory hosting the ledger database.
    """
    p.on_turn_start(1, "", author_id="alice")
    p.on_turn_start(2, "", author_id="bob")
    p.on_pre_compress([{"role": "user", "content": "cats preferred"}], require_checkpoint=True)
    assert p.wait_idle()
    assert p.store.count() == 0
    assert p.ledger.stats()["events"]["COMMITTED"] == 1


@pytest.mark.parametrize("mode", ["new", "reset", "branch", "rewind"])
def test_host_switch_clears_transients_and_preserves_durable_memory(tmp_path, monkeypatch, mode):
    """New/reset/branch/rewind invalidate recall without deleting durable facts."""
    p = provider(tmp_path)
    captured = capture_committed_event_payloads(monkeypatch, p.ledger)
    manager = MemoryManager()
    manager.add_provider(p)
    try:
        p.on_turn_start(1, "", author_id="alice")
        p.sync_turn("cats preferred", "ack")
        assert p.wait_idle()
        p.on_turn_start(2, "", author_id="bob")
        p.queue_prefetch("cats")
        assert p.wait_idle()
        target = "first" if mode in {"reset", "rewind"} else "second"
        manager.on_session_switch(
            target,
            user_id="alice",
            reset=mode in {"new", "reset"},
            rewound=mode == "rewind",
            parent_session_id="first" if mode == "branch" else "",
        )
        assert p.prefetch("cats") == ""
        assert p.store.count() == 1
        if mode in {"new", "reset"}:
            assert p._authors.get(target, set()) == set()
            assert target not in p._turns
        else:
            assert p._authors[target] == {"alice", "bob"}
            p.on_pre_compress([{"role": "user", "content": "dogs preferred"}])
            assert p.wait_idle()
            event = captured[-1]
            assert event["scope"]["user_id"] == "__mixed__"
            assert event["extract"] is False
    finally:
        p.shutdown()
