"""Fault-injection coverage for provider initialization boundaries."""

import json
import sqlite3
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient

import qdrant_memory.ledger as ledger_module
import qdrant_memory.provider as provider_module
from qdrant_memory.config import load_config
from qdrant_memory.ownership import WriterBusyError, WriterLease
from qdrant_memory.provider import ProviderUnavailableError, QdrantMemoryProvider
from qdrant_memory.qdrant_store import CollectionCompatibilityError
from tests.helpers import LLM, Embedder, config


class CloseTrackingEmbedder(Embedder):
    """Record closure of an injected embedder during setup cleanup."""

    def __init__(self):
        """Initialize the close counter."""
        self.close_calls = 0

    def close(self):
        """Count resource cleanup calls."""
        self.close_calls += 1


class FourDimensionalEmbedder:
    """Provide deterministic vectors for the persistent collection mismatch test."""

    dimensions = 4
    fingerprint = "test-embedding-space-v4"

    def embed_documents(self, texts):
        """Return one four-dimensional vector per input text."""
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def embed_query(self, _text):
        """Return a deterministic four-dimensional query vector."""
        return [1.0, 0.0, 0.0, 0.0]


OWNER_SETTINGS = {
    "document": {"destination": None, "clients": []},
    "policy": "unused-by-fault-fixture",
    "private_token_env": "QDRANT_MEMORY_TEST_OWNER_TOKEN",
    "home": ".",
    "enable_lifecycle": False,
}


@pytest.mark.parametrize(
    "stage",
    [
        "embedder_factory",
        "secret_lookup",
        "ledger_constructor",
        "ledger_partial_setup",
        "client_factory",
        "collection_validation",
        "native_owner_constructor",
        "native_owner_recovery",
        "native_owner_start",
        "worker_constructor",
    ],
)
def test_initialization_faults_leave_a_closed_diagnosable_provider(tmp_path, monkeypatch, stage):
    """Setup failures close attempt-owned resources and keep injected inputs retryable."""
    import qdrant_memory.native_owner as native_owner_module

    embedder = CloseTrackingEmbedder()
    client = None if stage == "client_factory" else QdrantClient(":memory:")
    client_close_calls = []
    if client is not None:
        close = client.close

        def tracked_close():
            client_close_calls.append("closed")
            close()

        monkeypatch.setattr(client, "close", tracked_close)

    provider = QdrantMemoryProvider(
        plugin_context=SimpleNamespace(llm=LLM()),
        embedder=None if stage == "embedder_factory" else embedder,
        client=client,
        overrides=config(),
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError(f"fault-injection:{stage}")

    if stage == "embedder_factory":
        monkeypatch.setattr(provider_module, "build_embedder", fail)
    elif stage == "secret_lookup":
        monkeypatch.setattr(
            native_owner_module,
            "load_native_settings",
            lambda *_args, **_kwargs: OWNER_SETTINGS,
        )

        class DummySharedOwner:
            def __init__(self, *_args, **_kwargs):
                pass

            def authenticate(self, _token):
                raise AssertionError("secret failure should occur before authentication")

        monkeypatch.setattr(native_owner_module, "SharedOwner", DummySharedOwner)
        monkeypatch.setattr(native_owner_module, "secret", fail)
    elif stage == "ledger_constructor":
        monkeypatch.setattr(provider_module, "Ledger", fail)
    elif stage == "ledger_partial_setup":
        connections = []
        connect = sqlite3.connect

        class TrackingConnection:
            def __init__(self, connection):
                object.__setattr__(self, "connection", connection)
                object.__setattr__(self, "close_calls", 0)

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def __setattr__(self, name, value):
                if name in {"connection", "close_calls"}:
                    object.__setattr__(self, name, value)
                else:
                    setattr(self.connection, name, value)

            def __enter__(self):
                self.connection.__enter__()
                return self

            def __exit__(self, *args):
                return self.connection.__exit__(*args)

            def close(self):
                self.close_calls += 1
                return self.connection.close()

        def tracked_connect(*args, **kwargs):
            connection = TrackingConnection(connect(*args, **kwargs))
            connections.append(connection)
            return connection

        monkeypatch.setattr(ledger_module.sqlite3, "connect", tracked_connect)
        monkeypatch.setattr(ledger_module.Ledger, "_scrub_terminal_payloads", fail)
    elif stage == "client_factory":
        monkeypatch.setattr(provider_module, "build_client", fail)
    elif stage == "collection_validation":
        monkeypatch.setattr(provider_module.QdrantStore, "initialize", fail)
    elif stage == "native_owner_constructor":
        monkeypatch.setattr(
            native_owner_module,
            "load_native_settings",
            lambda *_args, **_kwargs: OWNER_SETTINGS,
        )

        class FailingNativeOwner:
            def __init__(self, *_args, **_kwargs):
                fail()

        monkeypatch.setattr(native_owner_module, "NativeOwner", FailingNativeOwner)
    elif stage in {"native_owner_recovery", "native_owner_start"}:
        monkeypatch.setattr(
            native_owner_module,
            "load_native_settings",
            lambda *_args, **_kwargs: OWNER_SETTINGS,
        )
        closed_owners = []

        class FaultingNativeOwner:
            def __init__(self, *_args, **_kwargs):
                self.owner = SimpleNamespace(blocked=False)
                self.scope = SimpleNamespace(user_id="hermes-user", agent_id=None)

            def reauthorize_pending(self):
                pass

            def start(self):
                if stage == "native_owner_start":
                    fail()

            def close(self):
                closed_owners.append("closed")

        monkeypatch.setattr(native_owner_module, "NativeOwner", FaultingNativeOwner)
        if stage == "native_owner_recovery":
            monkeypatch.setattr(provider_module.Runtime, "recover", lambda _self: 1)
    elif stage == "worker_constructor":
        monkeypatch.setattr(provider_module, "spawn_context_thread", fail)

    expected_error = "Native Owner requires complete lifecycle recovery"
    if stage != "native_owner_recovery":
        expected_error = f"fault-injection:{stage}"
    with pytest.raises(RuntimeError, match=expected_error):
        provider.initialize("session", hermes_home=str(tmp_path))

    assert provider.lifecycle_state == "FAILED"
    assert provider.initialization_error["code"] == "initialization_failed"
    assert f"fault-injection:{stage}" not in json.dumps(provider.initialization_error)
    for field in ("cfg", "session_id", "default_scope", "ledger", "store", "runtime"):
        assert not hasattr(provider, field)
    assert provider._cache == {}
    assert provider._scopes == {}
    assert provider._turns == {}
    assert provider.prefetch("private session data") == ""
    provider.on_turn_start(1, "", session_id="failed-session")
    with pytest.raises(ProviderUnavailableError):
        provider.sync_turn("not durable", "not accepted")
    with pytest.raises(ProviderUnavailableError):
        provider.on_session_end([{"role": "user", "content": "private"}])
    with pytest.raises(ProviderUnavailableError):
        provider.on_pre_compress([], require_checkpoint=True)
    assert provider.on_pre_compress([], require_checkpoint=False) == ""
    tool_error = json.loads(provider.handle_tool_call("qdrant_memory_add", {"text": "x"}))["error"]
    assert tool_error["code"] == "initialization_failed"
    assert tool_error["type"] == "ProviderUnavailableError"
    assert f"fault-injection:{stage}" not in json.dumps(tool_error)

    if client is not None:
        assert client_close_calls == []
    if stage != "embedder_factory":
        assert embedder.close_calls == 0
    if stage == "ledger_partial_setup":
        assert len(connections) == 1
        assert connections[0].close_calls == 1
    if stage in {"native_owner_recovery", "native_owner_start"}:
        assert closed_owners == ["closed"]

    provider.shutdown()
    provider.shutdown()
    assert provider.lifecycle_state == "FAILED"
    if client is not None:
        assert client_close_calls == []
        client.close()
        assert client_close_calls == ["closed"]
    if stage != "embedder_factory":
        assert embedder.close_calls == 0
        embedder.close()
        assert embedder.close_calls == 1
    lease = WriterLease.for_config(tmp_path, load_config(str(tmp_path), config()))
    lease.close()


def test_failed_initialization_can_retry_with_injected_resources(tmp_path, monkeypatch):
    """A failed attempt keeps caller-injected client and embedder usable for retry."""
    embedder = CloseTrackingEmbedder()
    client = QdrantClient(":memory:")
    client_close_calls = []
    close = client.close

    def tracked_close():
        client_close_calls.append("closed")
        close()

    monkeypatch.setattr(client, "close", tracked_close)
    provider = QdrantMemoryProvider(
        plugin_context=SimpleNamespace(llm=LLM()),
        embedder=embedder,
        client=client,
        overrides=config(),
    )
    initialize = provider_module.QdrantStore.initialize
    attempts = 0

    def fail_once(store):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient collection inspection failure")
        return initialize(store)

    monkeypatch.setattr(provider_module.QdrantStore, "initialize", fail_once)
    with pytest.raises(RuntimeError, match="transient collection inspection failure"):
        provider.initialize("retry-session", hermes_home=str(tmp_path))

    assert provider.lifecycle_state == "FAILED"
    assert provider.client is client
    assert provider.embedder is embedder
    assert client_close_calls == []
    assert embedder.close_calls == 0

    provider.initialize("retry-session", hermes_home=str(tmp_path))
    provider.sync_turn("retry remains durable", "ack", session_id="retry-session")
    assert provider.wait_idle()
    provider.runtime.recover()
    provider.runtime.recover()
    assert provider.ledger.stats()["events"] == {"COMMITTED": 1}
    assert provider.store.count() == 1
    result = json.loads(
        provider.handle_tool_call("qdrant_memory_search", {"query": "retry remains durable"})
    )
    assert [memory["text"] for memory in result["memories"]] == ["retry remains durable"]

    provider.shutdown()
    assert client_close_calls == ["closed"]
    assert embedder.close_calls == 1


def test_native_owner_close_failure_keeps_writer_lease_until_cleanup_retry(tmp_path, monkeypatch):
    """A live Native Owner keeps its writer lease until a retry closes it."""
    import qdrant_memory.native_owner as native_owner_module

    monkeypatch.setattr(
        native_owner_module,
        "load_native_settings",
        lambda *_args, **_kwargs: OWNER_SETTINGS,
    )
    owners = []

    class CloseFailsOnce:
        def __init__(self, *_args, **_kwargs):
            self.owner = SimpleNamespace(blocked=False)
            self.scope = SimpleNamespace(user_id="hermes-user", agent_id=None)
            self.close_calls = 0
            owners.append(self)

        def reauthorize_pending(self):
            pass

        def start(self):
            pass

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise OSError("listener still active")

    monkeypatch.setattr(native_owner_module, "NativeOwner", CloseFailsOnce)
    provider = QdrantMemoryProvider(
        plugin_context=SimpleNamespace(llm=LLM()), embedder=Embedder(), overrides=config()
    )
    provider.initialize("owner-session", hermes_home=str(tmp_path))

    with pytest.raises(RuntimeError, match="cleanup failed"):
        provider.shutdown()
    assert provider.lifecycle_state == "FAILED"
    assert provider._cleanup_complete is False
    assert provider._native_owner is owners[0]
    assert provider._resources is not None
    assert owners[0].close_calls == 1

    contender = QdrantMemoryProvider(embedder=Embedder(), overrides=config())
    try:
        with pytest.raises(WriterBusyError):
            contender.initialize("contender", hermes_home=str(tmp_path))
    finally:
        contender.shutdown()

    provider.shutdown()
    assert provider.lifecycle_state == "FAILED"
    assert owners[0].close_calls == 2

    replacement_lease = WriterLease.for_config(tmp_path, load_config(str(tmp_path), config()))
    replacement_lease.close()


def test_vector_dimension_mismatch_during_provider_init_releases_ownership(tmp_path):
    """A real persistent collection mismatch fails before the provider becomes ready."""
    plugin_context = SimpleNamespace(llm=LLM())
    first = QdrantMemoryProvider(
        plugin_context=plugin_context,
        embedder=FourDimensionalEmbedder(),
        client=QdrantClient(path=str(tmp_path / "qdrant")),
        overrides=config(embedding={"dimensions": 4}),
    )
    first.initialize("initial-session", hermes_home=str(tmp_path))
    first.shutdown()

    second_client = QdrantClient(path=str(tmp_path / "qdrant"))
    second = QdrantMemoryProvider(
        plugin_context=plugin_context,
        embedder=Embedder(),
        client=second_client,
        overrides=config(),
    )
    with pytest.raises(CollectionCompatibilityError, match="dimension or distance mismatch"):
        second.initialize("mismatch-session", hermes_home=str(tmp_path))

    assert second.lifecycle_state == "FAILED"
    assert not hasattr(second, "runtime")
    assert second.client is second_client
    second.shutdown()
    second_client.close()
    lease = WriterLease.for_config(tmp_path, load_config(str(tmp_path), config()))
    lease.close()
