"""Production ownership excludes a second runtime during the replay write window."""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
import utils
from qdrant_client import QdrantClient

from qdrant_memory import cli
from qdrant_memory.config import load_config
from qdrant_memory.ownership import WriterBusyError, WriterLease
from qdrant_memory.provider import QdrantMemoryProvider

from .helpers import LLM, Embedder, config


@pytest.mark.parametrize("mode", ["embedded", "server", "cloud"])
def test_active_provider_excludes_second_runtime_before_service_access(tmp_path, monkeypatch, mode):
    """Provider, CLI and another process cannot enter an in-flight writer's destination."""
    settings = {"mode": mode}
    if mode != "embedded":
        settings["url"] = "https://writer.example.invalid"
    if mode == "cloud":
        monkeypatch.setattr("qdrant_memory.config.secret", lambda _name: "synthetic-test-key")
    owner = QdrantMemoryProvider(
        plugin_context=SimpleNamespace(llm=LLM()),
        embedder=Embedder(),
        client=QdrantClient(":memory:"),
        overrides=config(qdrant=settings),
    )
    owner.initialize("writer-session", hermes_home=str(tmp_path))
    assert owner.wait_idle()
    entered, release = Event(), Event()
    original = owner.store.upsert
    results = []

    def blocked_upsert(records):
        """Expose the exact interval after fence checks and before service confirmation."""
        entered.set()
        assert release.wait(15)
        return original(records)

    monkeypatch.setattr(owner.store, "upsert", blocked_upsert)
    thread = Thread(
        target=lambda: results.append(owner.handle_tool_call("qdrant_memory_add", {"text": "cats"}))
    )
    thread.start()
    try:
        assert entered.wait(5)

        def unexpected_service(*_args, **_kwargs):
            pytest.fail("Competing production writers must fail before service access")

        monkeypatch.setattr("qdrant_memory.provider.build_client", unexpected_service)
        contender = QdrantMemoryProvider(embedder=Embedder(), overrides=config(qdrant=settings))
        with pytest.raises(WriterBusyError):
            contender.initialize("contender", hermes_home=str(tmp_path))
        assert not hasattr(contender, "runtime")

        monkeypatch.setattr(cli, "load_config", lambda *_args, **_kwargs: owner.cfg)
        monkeypatch.setattr(cli, "build_embedder", unexpected_service)
        monkeypatch.setattr(cli, "build_client", unexpected_service)
        monkeypatch.setattr(
            cli,
            "migration_source",
            lambda *_args: ([{"id": "one", "memory": "dogs"}], "mem0-json", "fixture", None),
        )
        parser = argparse.ArgumentParser()
        cli.register_cli(parser)
        for arguments in (
            ["init", "--existing", "clear"],
            ["retry"],
            ["migrate", "mem0", "--source-json", "unused.json", "--quiet"],
        ):
            with pytest.raises(WriterBusyError):
                cli.run(parser.parse_args(arguments), home=tmp_path)

        # Exercise the actual OS lock in another process, independently of
        # Runtime.lock and of the embedded Qdrant engine's own storage lock.
        child = """
import json, sys
sys.path[:0] = sys.argv[3:]
from qdrant_memory.ownership import WriterBusyError, WriterLease
try:
    lease = WriterLease.for_config(sys.argv[1], json.loads(sys.argv[2]))
except WriterBusyError:
    sys.exit(23)
lease.close()
sys.exit(24)
"""
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                child,
                str(tmp_path),
                json.dumps(owner.cfg),
                str(Path(__file__).resolve().parents[1]),
                str(Path(utils.__file__).resolve().parent),
            ],
            timeout=10,
            check=False,
        )
        assert completed.returncode == 23
    finally:
        release.set()
        thread.join(10)
        owner.shutdown()
    assert not thread.is_alive()
    assert json.loads(results[0])["action"] == "ADD"


def test_writer_busy_provider_lifecycle_is_safe_and_reinitializable(tmp_path):
    """A failed initialize rejects writes explicitly and can retry after ownership releases."""
    owner = QdrantMemoryProvider(
        plugin_context=SimpleNamespace(llm=LLM()),
        embedder=Embedder(),
        client=QdrantClient(":memory:"),
        overrides=config(),
    )
    owner.initialize("owner-session", hermes_home=str(tmp_path))

    contender = QdrantMemoryProvider(
        plugin_context=SimpleNamespace(llm=LLM()),
        embedder=Embedder(),
        overrides=config(),
    )
    try:
        with pytest.raises(WriterBusyError):
            contender.initialize("contender-session", hermes_home=str(tmp_path))

        assert contender.lifecycle_state == "FAILED"
        assert contender.on_turn_start(1, "", session_id="contender-session") is None
        with pytest.raises(RuntimeError, match="unavailable"):
            contender.sync_turn("cats preferred", "ack", session_id="contender-session")
        assert contender.prefetch("cats", session_id="contender-session") == ""
        with pytest.raises(RuntimeError, match="unavailable"):
            contender.on_session_end([{"role": "user", "content": "cats"}])
        with pytest.raises(RuntimeError, match="unavailable"):
            contender.on_pre_compress([], require_checkpoint=True)
        error = json.loads(contender.handle_tool_call("qdrant_memory_add", {"text": "cats"}))[
            "error"
        ]
        assert error["code"] == "writer_busy"
        assert error["retryable"] is True
        assert "AttributeError" not in json.dumps(error)

        contender.shutdown()
        contender.shutdown()
        assert contender.lifecycle_state == "FAILED"
    finally:
        owner.shutdown()

    contender.initialize("contender-session", hermes_home=str(tmp_path))
    try:
        assert contender.lifecycle_state == "READY"
        assert contender.wait_idle()
        added = json.loads(
            contender.handle_tool_call("qdrant_memory_add", {"text": "cats preferred"})
        )
        assert added["action"] == "ADD"
        found = json.loads(contender.handle_tool_call("qdrant_memory_search", {"query": "cats"}))
        assert [item["text"] for item in found["memories"]] == ["cats preferred"]
        assert contender.store.count() == 1
    finally:
        contender.shutdown()


def test_configuration_failure_leaves_a_closed_diagnosable_provider(tmp_path, monkeypatch):
    """Configuration failure cannot expose uninitialized services or sessions."""
    provider = QdrantMemoryProvider(embedder=Embedder(), overrides=config())

    def fail_config(*_args, **_kwargs):
        raise ValueError("credential-sentinel")

    monkeypatch.setattr("qdrant_memory.provider.load_config", fail_config)
    with pytest.raises(ValueError, match="credential-sentinel"):
        provider.initialize("session", hermes_home=str(tmp_path))

    assert provider.lifecycle_state == "FAILED"
    assert provider.initialization_error == {
        "code": "initialization_failed",
        "type": "ValueError",
    }
    assert not hasattr(provider, "runtime")
    assert provider.prefetch("cats") == ""
    with pytest.raises(RuntimeError, match="unavailable"):
        provider.sync_turn("cats", "ack")
    provider.shutdown()
    provider.shutdown()


def test_collection_failure_releases_lease_and_injected_client(tmp_path, monkeypatch):
    """A post-lease setup exception closes the client and releases writer ownership."""
    client = QdrantClient(":memory:")
    closed = Event()
    close = client.close

    def close_client():
        closed.set()
        close()

    monkeypatch.setattr(client, "close", close_client)
    provider = QdrantMemoryProvider(
        plugin_context=SimpleNamespace(llm=LLM()),
        embedder=Embedder(),
        client=client,
        overrides=config(),
    )

    def fail_collection(_store):
        raise ValueError("collection fixture failure")

    monkeypatch.setattr("qdrant_memory.provider.QdrantStore.initialize", fail_collection)
    with pytest.raises(ValueError, match="collection fixture failure"):
        provider.initialize("session", hermes_home=str(tmp_path))

    assert provider.lifecycle_state == "FAILED"
    assert closed.is_set()
    assert not hasattr(provider, "runtime")
    lease = WriterLease.for_config(tmp_path, load_config(str(tmp_path), config()))
    lease.close()


def test_worker_start_failure_releases_every_resource(tmp_path, monkeypatch):
    """Worker startup is part of initialization and failure tears down the runtime."""
    worker = Thread(target=lambda: None)

    def fail_start():
        raise RuntimeError("worker start fixture failure")

    worker.start = fail_start
    provider = QdrantMemoryProvider(
        plugin_context=SimpleNamespace(llm=LLM()),
        embedder=Embedder(),
        client=QdrantClient(":memory:"),
        overrides=config(),
    )
    monkeypatch.setattr(
        "qdrant_memory.provider.spawn_context_thread", lambda *_args, **_kwargs: worker
    )

    with pytest.raises(RuntimeError, match="worker start fixture failure"):
        provider.initialize("session", hermes_home=str(tmp_path))

    assert provider.lifecycle_state == "FAILED"
    assert provider.initialization_error == {
        "code": "initialization_failed",
        "type": "RuntimeError",
    }
    lease = WriterLease.for_config(tmp_path, load_config(str(tmp_path), config()))
    lease.close()


def test_one_provider_serializes_delete_after_inflight_upsert(tmp_path, monkeypatch):
    """The supported single Runtime cannot delete between an UPSERT check and write."""
    owner = QdrantMemoryProvider(
        plugin_context=SimpleNamespace(llm=LLM()),
        embedder=Embedder(),
        client=QdrantClient(":memory:"),
        overrides=config(),
    )
    owner.initialize("writer-session", hermes_home=str(tmp_path))
    assert owner.wait_idle()
    created = json.loads(owner.handle_tool_call("qdrant_memory_add", {"text": "cats"}))
    identifier = created["id"]
    entered, release, delete_started, delete_finished = Event(), Event(), Event(), Event()
    original = owner.store.upsert
    results = []

    def blocked_upsert(records):
        entered.set()
        assert release.wait(10)
        return original(records)

    def delete():
        delete_started.set()
        results.append(owner.handle_tool_call("qdrant_memory_delete", {"id": identifier}))
        delete_finished.set()

    monkeypatch.setattr(owner.store, "upsert", blocked_upsert)
    updater = Thread(
        target=lambda: results.append(
            owner.handle_tool_call("qdrant_memory_update", {"id": identifier, "text": "dogs"})
        )
    )
    deleter = Thread(target=delete)
    updater.start()
    try:
        assert entered.wait(5)
        deleter.start()
        assert delete_started.wait(5)
        assert not delete_finished.wait(0.05)
        release.set()
        updater.join(10)
        deleter.join(10)
        assert not updater.is_alive() and not deleter.is_alive()
        assert {json.loads(result)["action"] for result in results} == {"UPDATE", "DELETE"}
        assert owner.store.get(identifier, owner.default_scope) is None
    finally:
        release.set()
        updater.join(10)
        if deleter.ident is not None:
            deleter.join(10)
        owner.shutdown()
