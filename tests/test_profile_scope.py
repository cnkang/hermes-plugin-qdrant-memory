"""Keep local inspection and session storage bound to their owning profile."""

import json
from types import SimpleNamespace

import pytest

from qdrant_memory.cli import run
from qdrant_memory.config import ledger_namespace, load_config

from .test_provider import provider


def test_relative_backend_path_is_profile_bound_and_canonical(tmp_path, monkeypatch):
    """Resolve relative storage against home and retain a stable ledger namespace."""
    home = tmp_path / "alice"
    other = tmp_path / "bob"
    home.mkdir()
    other.mkdir()
    overrides = {"qdrant": {"path": "data/../store"}}
    alice = load_config(home, overrides, resolve_secrets=False)
    bob = load_config(other, overrides, resolve_secrets=False)
    assert alice["qdrant"]["path"] == str(home / "store")
    assert bob["qdrant"]["path"] == str(other / "store")
    monkeypatch.chdir(other)
    absolute = load_config(home, {"qdrant": {"path": str(home / "store")}}, resolve_secrets=False)
    assert ledger_namespace(alice) == ledger_namespace(absolute)
    assert ledger_namespace(alice) != ledger_namespace(bob)


@pytest.mark.parametrize("ledger_exists", [False, True])
def test_cloud_status_without_secrets_is_local(tmp_path, monkeypatch, ledger_exists):
    """Report local Cloud settings while keeping other commands credential gated."""
    (tmp_path / "qdrant-memory.json").write_text(
        json.dumps({"qdrant": {"mode": "cloud", "url": "https://example.com"}})
    )
    monkeypatch.setattr("qdrant_memory.config.secret", lambda name: None)
    if ledger_exists:
        directory = tmp_path / "qdrant-memory"
        directory.mkdir()
        (directory / "state.db").touch()
    result = run(SimpleNamespace(qdrant_command="status"), home=tmp_path)
    assert result["mode"] == "cloud"
    assert result["ledger_exists"] is ledger_exists
    for command in ("stats", "doctor", "verify", "retry", "migrate"):
        with pytest.raises(ValueError):
            run(SimpleNamespace(qdrant_command=command), home=tmp_path)


@pytest.mark.parametrize("user_id", ["bob", None])
def test_switch_initializes_scope_before_first_turn(tmp_path, user_id):
    """A switch must not expose the previous user's memories before a turn hook."""
    p = provider(tmp_path)
    try:
        p.on_turn_start(1, "", author_id="alice")
        p.sync_turn("cats preferred", "ack")
        assert p.wait_idle()
        p.on_session_switch("second", user_id=user_id)
        result = json.loads(p.handle_tool_call("qdrant_memory_search", {"query": "cats"}))
        assert result["memories"] == []
        # A brand-new session still defaults when no user_id is supplied.
        expected = user_id or p.default_scope.user_id
        assert p._scope("second").user_id == expected
        p.on_session_switch("first", user_id="bob")
        assert p._scope("first").user_id == "bob"
    finally:
        p.shutdown()


def test_switch_back_without_user_id_preserves_recorded_scope(tmp_path):
    """Returning to a known session without user_id keeps its recorded author scope."""
    p = provider(tmp_path)
    try:
        p.on_turn_start(1, "", author_id="alice")
        p.sync_turn("cats preferred", "ack", session_id="first", turn_author={"id": "alice"})
        assert p.wait_idle()
        p.on_session_switch("second")
        p.on_session_switch("first", reset=False)
        assert p._scope("first").user_id == "alice"
        result = json.loads(
            p.handle_tool_call("qdrant_memory_search", {"query": "cats"}, session_id="first")
        )
        assert [m["text"] for m in result["memories"]] == ["cats preferred"]
    finally:
        p.shutdown()
