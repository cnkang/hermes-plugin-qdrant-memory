"""Keep timed-out workers from draining queued writes or losing ownership."""

from threading import Event, Thread

import pytest

from qdrant_memory.provider import QdrantMemoryProvider

from .helpers import Embedder, config
from .test_provider import provider


@pytest.mark.parametrize("boundary", ["llm", "qdrant"])
def test_shutdown_defers_queued_work_and_excludes_replacement(tmp_path, monkeypatch, boundary):
    """A blocked request retains its lease while queued durable events await replay."""
    p = provider(tmp_path)
    entered, release = Event(), Event()
    target, name = (
        (p.context.llm, "complete_structured") if boundary == "llm" else (p.store, "upsert")
    )
    original = getattr(target, name)

    def slow(*args, **kwargs):
        """Block one request until the test deliberately releases it."""
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, slow)
    p.cfg["write"]["shutdown_timeout_seconds"] = 0
    try:
        p.sync_turn("cats preferred", "ack")
        assert entered.wait(5)
        p.sync_turn("dogs preferred", "ack")
        p.queue_prefetch("cats")
        with pytest.raises(TimeoutError, match="ownership"):
            p.shutdown()
        assert not p._jobs
        contender = QdrantMemoryProvider(embedder=Embedder(), overrides=config())
        with pytest.raises(RuntimeError, match="writer"):
            contender.initialize("replacement", hermes_home=str(tmp_path))
    finally:
        release.set()
        p._worker.join(10)
    assert not p._worker.is_alive()
    recovered = provider(tmp_path)
    try:
        assert recovered.store.count() == 2
        assert recovered.ledger.stats()["events"]["COMMITTED"] == 2
    finally:
        recovered.shutdown()


def test_shutdown_keeps_foreground_tool_resources_alive(tmp_path, monkeypatch):
    """Cleanup waits for an admitted foreground tool before releasing its lease."""
    p = provider(tmp_path)
    entered, release = Event(), Event()
    original = p.store.search
    results = []

    def slow(*args, **kwargs):
        """Hold an active tool across the shutdown deadline."""
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(p.store, "search", slow)
    thread = Thread(
        target=lambda: results.append(p.handle_tool_call("qdrant_memory_search", {"query": "cats"}))
    )
    thread.start()
    try:
        assert entered.wait(5)
        p.cfg["write"]["shutdown_timeout_seconds"] = 0
        with pytest.raises(TimeoutError):
            p.shutdown()
        contender = QdrantMemoryProvider(embedder=Embedder(), overrides=config())
        with pytest.raises(RuntimeError, match="writer"):
            contender.initialize("replacement", hermes_home=str(tmp_path))
    finally:
        release.set()
        thread.join(10)
        p._worker.join(10)
    assert results == ['{"memories": []}']
    assert "error" in p.handle_tool_call("qdrant_memory_search", {"query": "cats"})


def test_writer_lease_covers_remote_mode_and_url_aliases(tmp_path):
    """Changing a Cloud/server label cannot overlap a retiring physical writer."""
    from qdrant_memory.ownership import WriterLease

    lease = WriterLease.for_config(
        tmp_path, config(qdrant={"mode": "server", "url": "https://example.com"})
    )
    try:
        cloud_config = config(qdrant={"mode": "cloud", "url": "https://EXAMPLE.com:443/"})
        with pytest.raises(RuntimeError, match="writer"):
            WriterLease.for_config(tmp_path, cloud_config)
    finally:
        lease.close()
