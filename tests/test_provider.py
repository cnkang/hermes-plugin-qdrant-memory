"""Primary-agent lifecycle, author isolation and durable turn persistence contracts."""

import json
from types import SimpleNamespace

from qdrant_memory.models import Scope
from qdrant_memory.provider import QdrantMemoryProvider

from .helpers import LLM, Embedder, config


def provider(home, llm=None):
    """Start an isolated provider and wait for initial recovery."""
    p = QdrantMemoryProvider(
        plugin_context=SimpleNamespace(llm=llm or LLM()), embedder=Embedder(), overrides=config()
    )
    p.initialize("first", hermes_home=str(home))
    assert p.wait_idle()
    return p


def test_100_fast_turns_no_loss_and_replay(tmp_path):
    """Verify 100 fast turns no loss and replay."""
    p = provider(tmp_path)
    for index in range(100):
        p.sync_turn(f"preference number {index}", "ack", session_id="first")
    assert p.wait_idle(60)
    assert p.ledger.stats()["events"]["COMMITTED"] == 100
    assert p.store.count() == 100
    for _ in range(10):
        p.sync_turn("preference number 0", "ack", session_id="first")
    assert p.wait_idle()
    assert p.store.count() == 100
    p.shutdown()


def test_sessions_authors_cache_and_builtin_previous_value(tmp_path):
    """Verify sessions authors cache and builtin previous value."""
    p = provider(tmp_path)
    p.on_turn_start(1, "", author_id="alice")
    p.sync_turn("cats preferred", "ack", session_id="first", turn_author={"id": "alice"})
    assert p.wait_idle()
    p.queue_prefetch("cats", session_id="first")
    assert p.wait_idle()
    assert "cats preferred" in p.prefetch("cats", session_id="first")
    p.on_session_switch("second")
    p.on_turn_start(1, "", author_id="bob")
    assert p.prefetch("cats", session_id="second") == ""
    result = json.loads(
        p.handle_tool_call("qdrant_memory_search", {"query": "cats"}, session_id="second")
    )
    assert result["memories"] == []
    p.on_memory_write("add", "user", "dogs preferred")
    p.on_memory_write("replace", "user", "dogs avoided", {"previous_content": "dogs preferred"})
    assert p.wait_idle()
    hits = p.store.search("dogs", Scope("bob", "hermes"))
    assert [h.payload["text"] for h in hits] == ["dogs avoided"]
    p.on_memory_write("remove", "user", "dogs avoided")
    assert p.wait_idle()
    assert (
        p.store.count(Scope("bob", "hermes")) == 1
    )  # missing authoritative previous value is safe
    p.on_memory_write("remove", "user", "", {"previous_content": "dogs avoided"})
    p.on_memory_write(
        "add", "user", "dogs avoided"
    )  # re-add after delete must be a new notification
    assert p.wait_idle()
    assert p.store.count(Scope("bob", "hermes")) == 1
    p.shutdown()


def test_raw_event_survives_failure_and_restart(tmp_path):
    """Verify raw event survives failure and restart."""

    class FailingLLM(LLM):
        """Simulate an extraction outage without remote model access."""

        def complete_structured(self, **kwargs):
            """Raise a controlled extraction failure for durable replay testing."""
            raise TimeoutError("sentinel-key-must-not-leak")

    p = provider(tmp_path, FailingLLM())
    p.sync_turn("cats preferred", "ack")
    assert p.wait_idle()
    assert p.ledger.stats()["events"]["FAILED"] == 1
    p.ledger.retry_failed()
    p.shutdown()
    restarted = provider(tmp_path)
    assert restarted.store.count() == 1
    assert restarted.ledger.stats()["events"]["COMMITTED"] == 1
    restarted.shutdown()


def test_nonprimary_skips_automatic_writes(tmp_path):
    """Verify nonprimary skips automatic writes."""
    p = QdrantMemoryProvider(
        plugin_context=SimpleNamespace(llm=LLM()), embedder=Embedder(), overrides=config()
    )
    p.initialize("subagent", hermes_home=str(tmp_path), agent_context="subagent")
    p.sync_turn("cats", "ack")
    p.on_session_end([{"role": "user", "content": "cats"}])
    assert p.wait_idle()
    assert p.store.count() == 0
    p.shutdown()
