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
from qdrant_memory.ownership import WriterBusyError
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
