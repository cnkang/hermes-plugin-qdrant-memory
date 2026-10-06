"""Regression contracts for verified review findings and initialization cleanup."""

from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient

from qdrant_memory import create_provider
from qdrant_memory.ledger import Ledger
from qdrant_memory.migration import map_record, migrate
from qdrant_memory.models import Scope
from qdrant_memory.provider import QdrantMemoryProvider
from scripts.evaluate import evaluate

from .helpers import LLM, Embedder, config, runtime


@pytest.mark.parametrize("status", ["PENDING", "FAILED"])
def test_unchanged_migration_supersedes_older_open_write(tmp_path, status):
    """Verify unchanged migration supersedes older open write."""
    rt = runtime(tmp_path)
    records = [{"id": "one", "memory": "current"}]
    first = migrate(rt, records, "mem0-json", "fixture")
    identifier = next(iter(first["records"]))
    _, stale = map_record({"id": "one", "memory": "stale"}, Scope(**rt.cfg["scope"]))
    key = rt.operation(identifier, "UPSERT", stale)
    if status == "FAILED":
        rt.ledger.failure("operations", key, ValueError("invalid"), terminal=True)
    result = migrate(rt, records, "mem0-json", "fixture")
    assert result["updated"] == 1
    assert rt.ledger.row("operations", key)["status"] == "SUPERSEDED"
    rt.ledger.retry_failed()
    rt.recover()
    assert next(rt.store.scroll()).payload["text"] == "current"
    rt.store.close()
    rt.ledger.close()


def test_missing_migration_operation_fails_explicitly(tmp_path):
    """Verify missing migration operation fails explicitly."""
    rt = runtime(tmp_path)
    records = [{"id": "one", "memory": "cats"}]
    manifest = migrate(rt, records, "mem0-json", "fixture")
    key = next(iter(manifest["records"].values()))["operation_key"]
    with rt.ledger.db:
        rt.ledger.db.execute("DELETE FROM operations WHERE idempotency_key=?", (key,))
    with pytest.raises(ValueError, match="Missing migration operation"):
        migrate(rt, records, "mem0-json", "fixture", resume=True, retry_failed=True)
    retained = rt.ledger.manifests()[-1]
    assert retained["failed"] == 1
    assert retained["completed_at"] is None
    rt.store.close()
    rt.ledger.close()


def test_open_operations_query_is_collection_scoped(tmp_path):
    """Verify open operations query is collection scoped."""
    own, other = Ledger(tmp_path, "own"), Ledger(tmp_path, "other")
    own.enqueue_operation("same-point", "UPSERT", {})
    assert own.has_open_operations("same-point")
    assert not other.has_open_operations("same-point")
    own.close()
    other.close()


def test_collector_registers_task_on_created_runtime_context(monkeypatch):
    """Verify collector registers task on created runtime context."""
    tasks = []
    ctx = SimpleNamespace(register_memory_provider=lambda provider: None)
    runtime_ctx = SimpleNamespace(
        llm=LLM(), register_auxiliary_task=lambda *args, **kwargs: tasks.append((args, kwargs))
    )
    monkeypatch.setattr("hermes_cli.plugins.PluginContext", lambda *args: runtime_ctx)
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: object())
    provider = create_provider(ctx)
    assert provider.context is runtime_ctx
    assert tasks[0][0] == ("qdrant_memory_extraction",)


@pytest.mark.parametrize("failure_stage", ["Ledger", "Runtime"])
def test_initialization_releases_resources_after_setup_failure(
    tmp_path, monkeypatch, failure_stage
):
    """Verify initialization releases resources after setup failure."""
    closed = []
    embedder = Embedder()
    embedder.close = lambda: closed.append("embedder")
    client = QdrantClient(path=str(tmp_path / "store"))
    p = QdrantMemoryProvider(
        SimpleNamespace(llm=LLM()), embedder=embedder, client=client, overrides=config()
    )

    def tracked_ledger(*args):
        """Track closure while retaining a real SQLite ledger."""
        ledger = Ledger(*args)
        original = ledger.close

        def close():
            """Record closure and release the real ledger connection."""
            closed.append("ledger")
            original()

        ledger.close = close
        return ledger

    monkeypatch.setattr("qdrant_memory.provider.Ledger", tracked_ledger)

    def fail(*args, **kwargs):
        """Inject a deterministic failure at the exercised boundary."""
        raise RuntimeError("setup failed")

    monkeypatch.setattr("qdrant_memory.provider." + failure_stage, fail)
    with pytest.raises(RuntimeError, match="setup failed"):
        p.initialize("session", hermes_home=str(tmp_path))
    assert "embedder" in closed
    if failure_stage == "Runtime":
        assert "ledger" in closed
    # Local persistence can be reopened only if the original client was closed.
    reopened = QdrantClient(path=str(tmp_path / "store"))
    reopened.close()


def test_retrieval_evaluation_handles_no_results():
    """Verify retrieval evaluation handles no results."""
    store = SimpleNamespace(upsert=lambda records: None, search=lambda *args, **kwargs: [])
    result = evaluate(
        {
            "memories": [{"id": "one", "text": "猫"}],
            "queries": [{"query": "猫", "relevant_ids": ["one"]}],
        },
        store,
        Scope("user", None),
    )
    assert result["precision_at_1"] == 0
    assert result["recall_at_10"] == 0
    assert result["mrr"] == 0
