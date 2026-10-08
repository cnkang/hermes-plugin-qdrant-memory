"""Crash recovery contracts for destructive, destination-scoped resets."""

import argparse
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import utils
from qdrant_client import QdrantClient, models
from utils import atomic_json_write

from qdrant_memory import cli
from qdrant_memory.config import ledger_namespace, load_config
from qdrant_memory.ledger import Ledger
from qdrant_memory.qdrant_store import QdrantStore, _close_embedded_collection_storage
from qdrant_memory.reset import (
    ResetRecoveryRequiredError,
    begin_destination_reset,
    has_pending_reset,
)
from tests.helpers import LLM, Embedder, config


def reset_fixture(home):
    """Create a populated destination plus unrelated durable ledger state."""
    atomic_json_write(
        home / "qdrant-memory.json",
        config(qdrant={"path": str(home / "qdrant")}),
        mode=0o600,
    )
    cfg = load_config(home)
    collection = cfg["qdrant"]["collection"]
    client = QdrantClient(path=cfg["qdrant"]["path"])
    client.create_collection(
        collection,
        vectors_config={"dense": models.VectorParams(size=3, distance=models.Distance.COSINE)},
    )
    client.upsert(
        collection,
        points=[
            models.PointStruct(
                id=42,
                vector={"dense": [1.0, 0.0, 0.0]},
                payload={"text": "old memory"},
            )
        ],
    )
    client.close()

    ledger = Ledger(home, ledger_namespace(cfg))
    ledger.enqueue_event({"session_id": "pending-session", "user": "queued"})
    ledger.enqueue_operation("queued", "UPSERT", {"text": "queued memory"})
    ledger.set_session_unattributed("quarantined-session")
    ledger.close()
    other = Ledger(home, "other-destination")
    other.enqueue_operation("other", "UPSERT", {"text": "keep"})
    other.close()
    return cfg


def parse_cli(*args):
    """Build CLI arguments for a maintenance command."""
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    return parser.parse_args(args)


def assert_other_destination_and_quarantine_survive(home, cfg):
    """Check reset cleanup does not cross destination or quarantine boundaries."""
    ledger = Ledger(home, ledger_namespace(cfg))
    assert ledger.unattributed_sessions() == {"quarantined-session"}
    ledger.close()
    other = Ledger(home, "other-destination")
    assert len(other.rows("operations")) == 1
    other.close()


def test_embedded_reset_closes_collection_storage_before_delete(tmp_path, monkeypatch):
    """Release SQLite handles before Qdrant removes the embedded collection directory."""
    cfg = reset_fixture(tmp_path)
    client = QdrantClient(path=cfg["qdrant"]["path"])
    store = QdrantStore(client, cfg, Embedder())
    local_collection = client._client.collections[cfg["qdrant"]["collection"]]
    original_close = local_collection.close
    original_delete = client.delete_collection
    calls = []

    def tracked_close():
        calls.append("close")
        original_close()

    def tracked_delete(collection_name, **kwargs):
        calls.append("delete")
        assert calls[-2:] == ["close", "delete"]
        return original_delete(collection_name, **kwargs)

    monkeypatch.setattr(local_collection, "close", tracked_close)
    monkeypatch.setattr(client, "delete_collection", tracked_delete)

    try:
        store.initialize(reset=True)

        assert calls == ["close", "delete"]
        assert store.client.retrieve(store.collection, ids=[42]) == []
    finally:
        store.close()


@pytest.mark.parametrize(
    ("cutpoint", "collection_exists_after_crash"),
    [("before-create", False), ("before-identity", True)],
)
def test_cli_resumes_reset_after_process_death(
    tmp_path, monkeypatch, cutpoint, collection_exists_after_crash
):
    """Keep the intent and replay rows until a reset validates, then resume on init."""
    cfg = reset_fixture(tmp_path)
    root = str(Path(__file__).resolve().parents[1])
    child = r"""
import os, sys
sys.path.insert(0, sys.argv[4])
sys.path.insert(0, sys.argv[3])
from argparse import ArgumentParser
from qdrant_client import QdrantClient
import qdrant_memory.cli as cli
from qdrant_memory.qdrant_store import QdrantStore
from tests.helpers import Embedder

cutpoint = sys.argv[2]
if cutpoint == "before-create":
    def crash_before_create(self, *args, **kwargs):
        os._exit(71)
    QdrantClient.create_collection = crash_before_create
else:
    def crash_before_identity(self, *args, **kwargs):
        os._exit(72)
    QdrantStore._validate_identity = crash_before_identity

cli.build_embedder = lambda context, cfg: Embedder()
parser = ArgumentParser()
cli.register_cli(parser)
cli.run(parser.parse_args(["init", "--existing", "clear"]), home=sys.argv[1])
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            child,
            str(tmp_path),
            cutpoint,
            root,
            str(Path(utils.__file__).resolve().parent),
        ],
        timeout=30,
    )
    assert completed.returncode == (71 if cutpoint == "before-create" else 72)

    client = QdrantClient(path=cfg["qdrant"]["path"])
    assert client.collection_exists(cfg["qdrant"]["collection"]) == collection_exists_after_crash
    client.close()
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    assert has_pending_reset(ledger)
    assert len(ledger.rows("events")) == 1
    assert len(ledger.rows("operations")) == 1
    ledger.close()
    assert_other_destination_and_quarantine_survive(tmp_path, cfg)

    # Other commands fail closed; init resumes the earlier explicit authorization,
    # including when the interrupted reset left no collection to prompt about.
    monkeypatch.setattr(cli, "build_embedder", lambda *args: pytest.fail("unexpected embedder"))
    with pytest.raises(ResetRecoveryRequiredError):
        cli.run(parse_cli("stats"), home=tmp_path)
    monkeypatch.setattr(cli, "build_embedder", lambda context, cfg: Embedder())
    result = cli.run(parse_cli("init"), home=tmp_path)
    assert result["ok"] and result["action"] == "clear"

    client = QdrantClient(path=cfg["qdrant"]["path"])
    assert client.collection_exists(cfg["qdrant"]["collection"])
    assert client.retrieve(cfg["qdrant"]["collection"], ids=[42]) == []
    client.close()
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    assert not has_pending_reset(ledger)
    assert ledger.rows("events") == []
    assert ledger.rows("operations") == []
    ledger.close()
    assert_other_destination_and_quarantine_survive(tmp_path, cfg)


def test_failed_collection_validation_retains_intent_and_replay_rows(tmp_path, monkeypatch):
    """A failed post-create identity check must leave reset recovery durable."""
    cfg = reset_fixture(tmp_path)

    def fail_identity(self, vector, create):
        raise ValueError("injected identity validation failure")

    monkeypatch.setattr(cli, "build_embedder", lambda context, cfg: Embedder())
    with monkeypatch.context() as patch:
        patch.setattr(QdrantStore, "_validate_identity", fail_identity)
        with pytest.raises(ValueError, match="injected identity validation failure"):
            cli.run(parse_cli("init", "--existing", "clear"), home=tmp_path)

    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    assert has_pending_reset(ledger)
    assert len(ledger.rows("events")) == 1
    assert len(ledger.rows("operations")) == 1
    ledger.close()
    assert_other_destination_and_quarantine_survive(tmp_path, cfg)

    result = cli.run(parse_cli("init"), home=tmp_path)
    assert result["ok"] and result["action"] == "clear"
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    assert not has_pending_reset(ledger)
    assert ledger.rows("events") == []
    assert ledger.rows("operations") == []
    ledger.close()
    assert_other_destination_and_quarantine_survive(tmp_path, cfg)


@pytest.mark.parametrize(
    ("failure_stage", "collection_exists_after_failure"),
    [
        ("probe", True),
        ("intent", True),
        ("delete", True),
        ("create", False),
        ("ledger-clear", True),
    ],
)
def test_reset_failures_preserve_data_or_a_resumable_intent(
    tmp_path, monkeypatch, failure_stage, collection_exists_after_failure
):
    """Every reset failure leaves the old target intact or an intent to resume."""
    cfg = reset_fixture(tmp_path)

    def fail(*_args, **_kwargs):
        raise RuntimeError(f"{failure_stage} injection")

    monkeypatch.setattr(cli, "build_embedder", lambda *_args: Embedder())
    with monkeypatch.context() as patch:
        if failure_stage == "probe":
            embedder = Embedder()
            patch.setattr(embedder, "embed_documents", fail)
            patch.setattr(cli, "build_embedder", lambda *_args: embedder)
        elif failure_stage == "intent":
            patch.setattr(cli, "begin_destination_reset", fail)
        elif failure_stage == "delete":
            patch.setattr(QdrantClient, "delete_collection", fail)
        elif failure_stage == "create":
            patch.setattr(QdrantClient, "create_collection", fail)
        else:
            patch.setattr(Ledger, "clear_destination", fail)

        with pytest.raises(RuntimeError, match=failure_stage):
            cli.run(parse_cli("init", "--existing", "clear"), home=tmp_path)

    client = QdrantClient(path=cfg["qdrant"]["path"])
    assert client.collection_exists(cfg["qdrant"]["collection"]) is collection_exists_after_failure
    if collection_exists_after_failure:
        assert bool(client.retrieve(cfg["qdrant"]["collection"], ids=[42])) is (
            failure_stage in {"probe", "intent", "delete"}
        )
    client.close()

    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    assert has_pending_reset(ledger) is (failure_stage != "intent")
    assert len(ledger.rows("events")) == 1
    assert len(ledger.rows("operations")) == 1
    ledger.close()
    assert_other_destination_and_quarantine_survive(tmp_path, cfg)

    retry_args = (
        parse_cli("init", "--existing", "clear") if failure_stage == "intent" else parse_cli("init")
    )
    result = cli.run(retry_args, home=tmp_path)
    assert result["ok"] and result["action"] == "clear"
    client = QdrantClient(path=cfg["qdrant"]["path"])
    assert client.collection_exists(cfg["qdrant"]["collection"])
    assert client.retrieve(cfg["qdrant"]["collection"], ids=[42]) == []
    client.close()
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    assert not has_pending_reset(ledger)
    assert ledger.rows("events") == []
    assert ledger.rows("operations") == []
    ledger.close()
    assert_other_destination_and_quarantine_survive(tmp_path, cfg)


def test_provider_resumes_reset_before_queuing_ledger_replay(tmp_path, monkeypatch):
    """Provider recovery must observe reset completion before it queues replay."""
    from qdrant_memory.provider import QdrantMemoryProvider

    cfg = reset_fixture(tmp_path)
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    begin_destination_reset(ledger)
    ledger.close()
    client = QdrantClient(path=cfg["qdrant"]["path"])
    _close_embedded_collection_storage(client, cfg["qdrant"]["collection"])
    from qdrant_client.local import qdrant_local

    with monkeypatch.context() as patch:
        # Model Windows' ignored rmtree failure after Qdrant already removed the
        # collection from its catalog, leaving its SQLite directory orphaned.
        patch.setattr(qdrant_local.shutil, "rmtree", lambda *_args, **_kwargs: None)
        client.delete_collection(cfg["qdrant"]["collection"])
    client.close()
    collection_path = Path(cfg["qdrant"]["path"]) / "collection" / cfg["qdrant"]["collection"]
    assert collection_path.is_dir()

    provider = QdrantMemoryProvider(
        plugin_context=SimpleNamespace(llm=LLM()),
        embedder=Embedder(),
        overrides=config(qdrant={"path": cfg["qdrant"]["path"]}),
    )
    observed = []
    original_run_job = provider._run_job

    def observe_before_job(kind, value):
        observed.append(
            (
                kind,
                has_pending_reset(provider.ledger),
                len(provider.ledger.rows("events")),
                len(provider.ledger.rows("operations")),
            )
        )
        return original_run_job(kind, value)

    monkeypatch.setattr(provider, "_run_job", observe_before_job)
    try:
        provider.initialize("session", hermes_home=str(tmp_path))
        assert provider.wait_idle()
        assert observed == [("recover", False, 0, 0)]
        assert provider.store.client.retrieve(cfg["qdrant"]["collection"], ids=[42]) == []
    finally:
        if provider._worker is not None:
            provider.shutdown()
    assert_other_destination_and_quarantine_survive(tmp_path, cfg)
