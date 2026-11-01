"""Keep timed-out workers from draining queued writes or losing ownership."""

from threading import Event, Thread
from types import SimpleNamespace

import pytest

from qdrant_memory.provider import QdrantMemoryProvider

from .helpers import LLM, Embedder, config
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


def test_shutdown_during_initialize_cancels_before_ready_and_releases_lease(tmp_path, monkeypatch):
    """Concurrent shutdown cancels setup and waits for its lease cleanup."""
    entered, release = Event(), Event()
    provider = QdrantMemoryProvider(
        plugin_context=SimpleNamespace(llm=LLM()),
        overrides=config(),
    )
    initialize_errors = []
    shutdown_errors = []

    def blocked_embedder(*_args, **_kwargs):
        entered.set()
        assert release.wait(10)
        return Embedder()

    def initialize():
        try:
            provider.initialize("session", hermes_home=str(tmp_path))
        except Exception as exc:
            initialize_errors.append(exc)

    def shutdown():
        try:
            provider.shutdown()
        except Exception as exc:
            shutdown_errors.append(exc)

    monkeypatch.setattr("qdrant_memory.provider.build_embedder", blocked_embedder)
    initializer = Thread(target=initialize)
    initializer.start()
    assert entered.wait(5)
    stopper = Thread(target=shutdown)
    stopper.start()
    with provider._condition:
        assert provider._condition.wait_for(lambda: provider._shutdown_requested, timeout=5)
    release.set()
    initializer.join(15)
    stopper.join(15)

    assert not initializer.is_alive() and not stopper.is_alive()
    assert not shutdown_errors
    assert len(initialize_errors) == 1
    assert str(initialize_errors[0]) == "Provider initialization was stopped"
    assert provider.lifecycle_state == "STOPPED"
    assert provider.prefetch("cats") == ""

    from qdrant_memory.config import load_config
    from qdrant_memory.ownership import WriterLease

    lease = WriterLease.for_config(tmp_path, load_config(str(tmp_path), config()))
    lease.close()


def test_shutdown_waits_for_early_initialization_failure_cleanup(tmp_path, monkeypatch):
    """A concurrent shutdown must wait when configuration fails before cfg exists."""
    load_entered, release_load = Event(), Event()
    cleanup_entered, release_cleanup = Event(), Event()
    shutdown_finished = Event()
    provider = QdrantMemoryProvider(
        plugin_context=SimpleNamespace(llm=LLM()),
        overrides=config(),
    )
    initialize_errors = []
    shutdown_errors = []
    original_dispose = provider._dispose_resources

    def fail_configuration(*_args, **_kwargs):
        load_entered.set()
        assert release_load.wait(10)
        raise ValueError("invalid test configuration")

    def blocked_dispose(**kwargs):
        cleanup_entered.set()
        assert release_cleanup.wait(10)
        return original_dispose(**kwargs)

    def initialize():
        try:
            provider.initialize("session", hermes_home=str(tmp_path))
        except Exception as exc:
            initialize_errors.append(exc)

    def shutdown():
        try:
            provider.shutdown()
        except Exception as exc:
            shutdown_errors.append(exc)
        finally:
            shutdown_finished.set()

    monkeypatch.setattr("qdrant_memory.provider.load_config", fail_configuration)
    monkeypatch.setattr(provider, "_dispose_resources", blocked_dispose)
    initializer = Thread(target=initialize)
    initializer.start()
    assert load_entered.wait(5)
    stopper = Thread(target=shutdown)
    stopper.start()
    with provider._condition:
        assert provider._condition.wait_for(lambda: provider._shutdown_requested, timeout=5)

    release_load.set()
    assert cleanup_entered.wait(5)
    assert not shutdown_finished.wait(0.2)
    release_cleanup.set()
    initializer.join(15)
    stopper.join(15)

    assert not initializer.is_alive() and not stopper.is_alive()
    assert len(initialize_errors) == 1
    assert str(initialize_errors[0]) == "Provider initialization was stopped"
    assert shutdown_errors == []
    assert provider.lifecycle_state == "STOPPED"


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
