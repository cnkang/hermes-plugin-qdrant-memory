"""Exercise the plugin through Hermes's real background memory manager."""

import os
import threading
import time

import pytest

from .test_provider import provider


def _memory_manager_class():
    """Load the host's lifecycle API, requiring it in the latest-upstream lane."""
    required = os.environ.get("HERMES_QDRANT_REQUIRE_HOST_SYNC") == "1"
    try:
        import agent.memory_manager as memory_manager

        MemoryManager = memory_manager.MemoryManager
    except ImportError as exc:
        if required:
            pytest.fail(f"Latest Hermes host has no MemoryManager: {exc}")
        pytest.skip("Hermes host does not expose MemoryManager")

    methods = ("add_provider", "sync_all", "shutdown_all")
    missing = [method for method in methods if not callable(getattr(MemoryManager, method, None))]
    if not hasattr(MemoryManager, "shutdown_drain_state"):
        missing.append("shutdown_drain_state")
    if not hasattr(memory_manager, "_SYNC_DRAIN_TIMEOUT_S"):
        missing.append("_SYNC_DRAIN_TIMEOUT_S")
    if missing:
        message = f"Hermes host lacks memory lifecycle methods: {', '.join(missing)}"
        if required:
            pytest.fail(message)
        pytest.skip(message)
    return MemoryManager


@pytest.mark.parametrize("required", [False, True])
def test_missing_sync_drain_timeout_uses_lane_requirement(monkeypatch, required):
    """Skip or fail cleanly when the host lacks the timeout used by monkeypatch."""
    try:
        import agent.memory_manager as memory_manager
    except ImportError:
        pytest.skip("Hermes host does not expose MemoryManager")

    monkeypatch.delattr(memory_manager, "_SYNC_DRAIN_TIMEOUT_S", raising=False)
    if required:
        monkeypatch.setenv("HERMES_QDRANT_REQUIRE_HOST_SYNC", "1")
        with pytest.raises(pytest.fail.Exception, match="_SYNC_DRAIN_TIMEOUT_S"):
            _memory_manager_class()
    else:
        monkeypatch.delenv("HERMES_QDRANT_REQUIRE_HOST_SYNC", raising=False)
        with pytest.raises(pytest.skip.Exception, match="_SYNC_DRAIN_TIMEOUT_S"):
            _memory_manager_class()


def test_host_submit_queue_shutdown_and_restart(tmp_path):
    """Persist a normal host-submitted turn before provider shutdown and restart."""
    manager_type = _memory_manager_class()
    memory = provider(tmp_path)
    manager = manager_type()
    manager.add_provider(memory)

    manager.sync_all(
        "Remember that the preferred deployment region is Singapore.",
        "I will keep that preference in mind.",
        session_id="host-lifecycle-session",
    )
    manager.shutdown_all()

    restarted = provider(tmp_path)
    try:
        statuses = restarted.ledger.stats()["events"]
        assert statuses["COMMITTED"] == 1
    finally:
        restarted.shutdown()


def test_host_shutdown_reports_turns_not_yet_admitted_to_provider(tmp_path, monkeypatch):
    """Show a canceled host-queue callback has no plugin ledger row to replay."""
    manager_type = _memory_manager_class()
    monkeypatch.setattr("agent.memory_manager._SYNC_DRAIN_TIMEOUT_S", 0.02)
    memory = provider(tmp_path)
    manager = manager_type()
    manager.add_provider(memory)
    callback_started = threading.Event()
    callback_finished = threading.Event()
    release_callback = threading.Event()

    def hold_before_plugin_admission(
        user_content, assistant_content, *, session_id="", messages=None, turn_author=None
    ):
        """Hold the host callback before entering the plugin's synchronous ledger insert."""
        if user_content == "host-callback-held-before-plugin":
            callback_started.set()
            release_callback.wait(timeout=3)
            callback_finished.set()

    memory.sync_turn = hold_before_plugin_admission
    manager.sync_all("host-callback-held-before-plugin", "first", session_id="held")
    assert callback_started.wait(timeout=3)
    manager.sync_all("still-queued-in-hermes", "second", session_id="queued")

    shutdown_finished = threading.Event()

    def shutdown():
        """Run the normal bounded host shutdown path on its own thread."""
        manager.shutdown_all()
        shutdown_finished.set()

    shutdown_thread = threading.Thread(target=shutdown, daemon=True)
    shutdown_thread.start()
    deadline = time.monotonic() + 3
    state = manager.shutdown_drain_state
    while state["status"] != "timed_out" and time.monotonic() < deadline:
        time.sleep(0.005)
        state = manager.shutdown_drain_state

    release_callback.set()
    assert state["status"] == "timed_out"
    assert state["abandoned_writes"] == 1
    assert callback_finished.wait(timeout=3)
    assert shutdown_finished.wait(timeout=3)
    shutdown_thread.join(timeout=3)

    restarted = provider(tmp_path)
    try:
        assert sum(restarted.ledger.stats()["events"].values()) == 0
    finally:
        restarted.shutdown()
