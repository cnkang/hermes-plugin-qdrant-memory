"""Defend transcript attribution across restart, continuation and storage recovery."""

import json

import pytest

from qdrant_memory.ledger import Ledger
from qdrant_memory.models import Scope

from .helpers import capture_committed_event_payloads
from .test_provider import provider


def assert_neutral_transcript(p, monkeypatch):
    """Unknown or mixed historical messages must never create personal memories."""
    captured = capture_committed_event_payloads(monkeypatch, p.ledger)
    messages = [{"role": "user", "content": "historical private preference"}]
    before = p.store.count()
    p.on_session_end(messages)
    p.on_pre_compress(messages, require_checkpoint=True)
    assert p.wait_idle()
    assert p.store.count() == before
    assert len(captured) == 1
    assert captured[0]["extract"] is False
    assert captured[0]["scope"] == {"user_id": "__mixed__", "agent_id": None}


@pytest.mark.parametrize("captured_only", [False, True])
def test_mixed_authors_remain_private_after_restart(tmp_path, monkeypatch, captured_only):
    """Both callback paths retain earlier principals after the process is replaced."""
    p = provider(tmp_path)
    p.on_session_switch("first", reset=True, user_id="alice")
    for number, author in enumerate(("alice", "bob"), 1):
        if captured_only:
            p.sync_turn("", "", turn_author={"id": author})
        else:
            p.on_turn_start(number, "", author_id=author)
    assert p.wait_idle()
    p.shutdown()
    p = provider(tmp_path)
    try:
        p.on_turn_start(3, "", author_id="bob")
        assert p._authors["first"] == {"alice", "bob"}
        assert_neutral_transcript(p, monkeypatch)
        assert p.store.count(Scope("bob", "hermes")) == 0
    finally:
        p.shutdown()


@pytest.mark.parametrize("mode", ["new_author", "scope_switch", "branch", "rewind"])
def test_single_author_history_cannot_be_reassigned(tmp_path, monkeypatch, mode):
    """A changed current principal never proves ownership of an older transcript."""
    p = provider(tmp_path)
    p.on_session_switch("first", reset=True, user_id="alice")
    p.on_turn_start(1, "", author_id="alice")
    p.shutdown()
    p = provider(tmp_path)
    try:
        if mode == "new_author":
            p.on_turn_start(2, "", author_id="bob")
        else:
            p.on_session_switch(
                "child" if mode == "branch" else "first",
                user_id="bob",
                parent_session_id="first" if mode == "branch" else None,
                rewound=mode == "rewind",
            )
        assert_neutral_transcript(p, monkeypatch)
    finally:
        p.shutdown()


def test_continuation_provenance_and_default_turns_survive_restart(tmp_path, monkeypatch):
    """Fallback CLI principals stay usable but cannot certify another user's history."""
    p = provider(tmp_path)
    for number in (1, 2):
        p.on_turn_start(number, "")
        p.sync_turn(f"ordinary preference {number}", "ack")
    assert p.wait_idle()
    p.on_turn_start(3, "", author_id="alice")
    p.on_session_switch("child", parent_session_id="first")
    p.shutdown()
    p = provider(tmp_path)
    try:
        p.on_session_switch("child")
        assert p._authors["child"] == {p.default_scope.user_id, "alice"}
        assert_neutral_transcript(p, monkeypatch)
    finally:
        p.shutdown()


def test_unknown_old_history_requires_explicit_transcript_reset(tmp_path, monkeypatch):
    """Scrubbed legacy events and unseen history fail closed until a trusted reset."""
    p = provider(tmp_path)
    p.sync_turn("older private preference", "ack")
    assert p.wait_idle()
    # Simulate an older ledger without provenance metadata, retaining scrubbed events.
    with p.ledger.db:
        p.ledger.db.execute("DELETE FROM session_provenance")
    p.shutdown()
    p = provider(tmp_path)
    try:
        p.on_turn_start(2, "", author_id="bob")
        assert_neutral_transcript(p, monkeypatch)
        p.on_session_switch("first", reset=True, user_id="bob")
        p.on_session_end([{"role": "user", "content": "fresh personal preference"}])
        assert p.wait_idle()
        assert p.store.count(Scope("bob", "hermes")) == 1
    finally:
        p.shutdown()


def test_author_persistence_failure_revokes_recall(tmp_path, monkeypatch):
    """An admission failure cannot leave the new caller using the previous scope."""
    p = provider(tmp_path)
    try:
        p.on_turn_start(1, "", author_id="alice")
        original = p.ledger.record_session_author

        def fail(*args, **kwargs):
            """Simulate unavailable durable attribution storage."""
            raise OSError("attribution unavailable")

        monkeypatch.setattr(p.ledger, "record_session_author", fail)
        with pytest.raises(OSError, match="attribution unavailable"):
            p.on_turn_start(2, "", author_id="bob")
        assert p._authors["first"] == {"alice"}
        assert json.loads(p.handle_tool_call("qdrant_memory_search", {"query": "fact"}))["error"]
        monkeypatch.setattr(p.ledger, "record_session_author", original)
        p.on_turn_start(3, "", author_id="bob")
        assert p._authors["first"] == {"alice", "bob"}
    finally:
        p.shutdown()


def test_destination_reset_preserves_provenance_and_namespace_isolation(tmp_path):
    """Clearing replay work must not erase transcript ownership in any destination."""
    ledger = Ledger(tmp_path, "one")
    try:
        ledger.reset_session_provenance("session", "alice")
        ledger.record_session_author("session", "bob", explicit=True)
        ledger.inherit_session_provenance("child", "session")
        ledger.clear_destination()
        assert ledger.session_provenance()["child"]["authors"] == {"alice", "bob"}
    finally:
        ledger.close()
    other = Ledger(tmp_path, "two")
    try:
        assert other.session_provenance() == {}
        other.record_session_author("session", "other")
    finally:
        other.close()
    ledger = Ledger(tmp_path, "one")
    try:
        assert ledger.session_provenance()["session"]["authors"] == {"alice", "bob"}
    finally:
        ledger.close()


def test_continuation_copies_quarantine_in_same_transaction(tmp_path, monkeypatch):
    """Partial continuation admission must never publish trusted child history."""
    ledger = Ledger(tmp_path, "destination")
    try:
        ledger.reset_session_provenance("parent", "alice")
        ledger.set_session_unattributed("parent")
        persist = ledger._save_session_provenance

        def fail_after_write(session, value):
            """Interrupt a transition before its locked transaction commits."""
            persist(session, value)
            raise OSError("transaction unavailable")

        monkeypatch.setattr(ledger, "_save_session_provenance", fail_after_write)
        with pytest.raises(OSError, match="transaction unavailable"):
            ledger.inherit_session_provenance("child", "parent")
        assert "child" not in ledger.session_provenance()
        assert ledger.unattributed_sessions() == {"parent"}
        monkeypatch.setattr(ledger, "_save_session_provenance", persist)
        ledger.inherit_session_provenance("child", "parent")
        assert ledger.unattributed_sessions() == {"parent", "child"}
    finally:
        ledger.close()


def test_known_single_author_and_default_cli_remain_usable(tmp_path):
    """A proven same-principal transcript and ordinary CLI turns remain supported."""
    p = provider(tmp_path)
    p.on_session_switch("first", reset=True, user_id="alice")
    p.on_turn_start(1, "", author_id="alice")
    p.shutdown()
    p = provider(tmp_path)
    try:
        p.on_turn_start(2, "", author_id="alice")
        assert p._transcript_scope() is None
        p.on_session_switch("first", reset=True, user_id="alice")
        p.on_session_end([{"role": "user", "content": "same author preference"}])
        assert p.wait_idle()
        assert p.store.count(Scope("alice", "hermes")) == 1
        p.on_session_switch("fresh", reset=True)
        for number in (1, 2):
            p.on_turn_start(number, "")
            p.sync_turn(f"CLI preference {number}", "ack")
        assert p.wait_idle()
        assert p.store.count(p.default_scope) == 2
    finally:
        p.shutdown()


@pytest.mark.parametrize("restart", [False, True])
def test_failed_admission_never_recertifies_history(tmp_path, monkeypatch, restart):
    """A suppressed/interrupted author callback leaves aggregate extraction disabled."""
    p = provider(tmp_path)
    p.on_session_switch("first", reset=True, user_id="alice")
    persist = p.ledger.record_session_author

    def fail(*args, **kwargs):
        """Model unavailable author admission storage."""
        raise OSError("author admission unavailable")

    monkeypatch.setattr(p.ledger, "record_session_author", fail)
    with pytest.raises(OSError, match="author admission unavailable"):
        p.on_turn_start(1, "", author_id="bob")
    monkeypatch.setattr(p.ledger, "record_session_author", persist)
    if restart:
        p.shutdown()
        p = provider(tmp_path)
    try:
        p.on_turn_start(2, "", author_id="alice")
        assert_neutral_transcript(p, monkeypatch)
        p.on_session_switch("child", parent_session_id="first")
        assert_neutral_transcript(p, monkeypatch)
    finally:
        p.shutdown()


@pytest.mark.parametrize("reset", [False, True])
def test_transition_persistence_failure_denies_old_authority(tmp_path, monkeypatch, reset):
    """Failed reset/continuation cannot leave a new host session using old recall."""
    p = provider(tmp_path)
    try:
        p.on_turn_start(1, "", author_id="alice")

        def fail(*args, **kwargs):
            """Simulate a durable transition failure."""
            raise OSError("transition unavailable")

        monkeypatch.setattr(
            p.ledger, "reset_session_provenance" if reset else "inherit_session_provenance", fail
        )
        with pytest.raises(OSError, match="transition unavailable"):
            p.on_session_switch("child", reset=reset, parent_session_id="first", user_id="bob")
        assert p.session_id == "first"
        assert p._authors["first"] == {"alice"}
        assert {"first", "child"} <= p._blocked_sessions
        assert json.loads(p.handle_tool_call("qdrant_memory_search", {"query": "fact"}))["error"]
    finally:
        p.shutdown()
