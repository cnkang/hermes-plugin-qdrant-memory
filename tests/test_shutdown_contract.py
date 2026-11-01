"""Keep timed-out workers from draining queued writes or losing ownership."""

import json
from threading import Event, Thread
from types import SimpleNamespace

import pytest

import qdrant_memory.provider as provider_module
from qdrant_memory.config import load_config
from qdrant_memory.ownership import WriterBusyError, WriterLease
from qdrant_memory.provider import ProviderUnavailableError, QdrantMemoryProvider

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


def test_worker_exit_closes_admission_before_resource_cleanup(tmp_path, monkeypatch):
    """An unexpectedly exiting worker becomes unavailable before closing storage."""
    p = provider(tmp_path)
    assert p.wait_idle()
    worker = p._worker
    entered, release = Event(), Event()
    resources = p._resources
    close = resources.close

    def blocked_close():
        entered.set()
        assert release.wait(10)
        return close()

    monkeypatch.setattr(resources, "close", blocked_close)
    try:
        with p._condition:
            p._accepting = False
            p._condition.notify_all()
        assert entered.wait(5)
        assert p.lifecycle_state == "STOPPING"
        assert p.prefetch("private old-session content") == ""
        with pytest.raises(ProviderUnavailableError):
            p.sync_turn("must not be acknowledged", "ack")
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert p.lifecycle_state == "FAILED"
    assert p.initialization_error["code"] == "worker_stopped"


def test_worker_exit_replays_durable_pending_event_after_explicit_retry(tmp_path, monkeypatch):
    """An accepted Ledger event survives worker exit and is replayed once on retry."""
    p = provider(tmp_path)
    assert p.wait_idle()
    worker = p._worker
    entered, release = Event(), Event()
    original_run_job = p._run_job
    session = p.session_id

    def fail_after_event_is_durable(kind, value):
        if kind == "event":
            entered.set()
            assert release.wait(10)
            with p._condition:
                p._accepting = False
                p._condition.notify_all()
            raise RuntimeError("injected worker exit after durable event")
        return original_run_job(kind, value)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(p, "_run_job", fail_after_event_is_durable)
            p.sync_turn("durable pending turn", "ack", session_id=session)
            assert entered.wait(5)
            assert p.ledger.stats()["events"] == {"PENDING": 1}
            release.set()
            worker.join(10)
            assert not worker.is_alive()
    finally:
        release.set()
        if worker.is_alive():
            worker.join(10)

    assert p.lifecycle_state == "FAILED"
    assert p.initialization_error["code"] == "worker_stopped"
    p.initialize(session, hermes_home=str(tmp_path))
    assert p.wait_idle()
    p.runtime.recover()
    p.runtime.recover()
    assert p.ledger.stats()["events"] == {"COMMITTED": 1}
    assert p.store.count() == 1
    result = json.loads(p.handle_tool_call("qdrant_memory_search", {"query": "durable pending"}))
    assert [memory["text"] for memory in result["memories"]] == ["durable pending turn"]
    p.shutdown()


def test_resource_cleanup_failure_retains_lease_until_failed_callback_retries(tmp_path):
    """A failed cleanup callback remains retryable and cannot release the writer lease."""
    p = provider(tmp_path)
    attempts = 0

    def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected close failure")

    p._resources.callback(fail_once)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        p.shutdown()
    assert p.lifecycle_state == "FAILED"
    assert p._cleanup_complete is False
    assert attempts == 1

    with pytest.raises(WriterBusyError):
        WriterLease.for_config(tmp_path, load_config(str(tmp_path), config()))

    p.shutdown()
    assert p.lifecycle_state == "FAILED"
    assert p._cleanup_complete is True
    assert attempts == 2
    lease = WriterLease.for_config(tmp_path, load_config(str(tmp_path), config()))
    lease.close()


def test_initialize_preserves_stopped_state_when_shutdown_wins_worker_start_race(
    tmp_path, monkeypatch
):
    """A completed concurrent shutdown cannot be overwritten back to STOPPING."""
    p = QdrantMemoryProvider(
        plugin_context=SimpleNamespace(llm=LLM()), embedder=Embedder(), overrides=config()
    )

    class ShutdownWonWorkerStart:
        ident = None

        def start(self):
            with p._condition:
                p._shutdown_requested = True
            p._dispose_resources(final_state="STOPPED")

    monkeypatch.setattr(
        provider_module,
        "spawn_context_thread",
        lambda *_args, **_kwargs: ShutdownWonWorkerStart(),
    )
    with pytest.raises(RuntimeError, match="initialization was stopped"):
        p.initialize("stopped-race", hermes_home=str(tmp_path))

    assert p.lifecycle_state == "STOPPED"
    p.shutdown()
