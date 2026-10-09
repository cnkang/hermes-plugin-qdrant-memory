"""Memory inventory, portable export and scoped deletion contracts."""

import argparse
import json

import pytest

from qdrant_memory.cli import (
    ALL_AGENTS,
    DeleteConfirmationRequiredError,
    ScopeSelectionError,
    main,
    register_cli,
    run,
)
from qdrant_memory.config import ledger_namespace, load_config
from qdrant_memory.inventory import ExportTargetExistsError
from qdrant_memory.ledger import Ledger
from qdrant_memory.models import Scope, payload
from qdrant_memory.reset import (
    ScopeDeleteRecoveryRequiredError,
    ScopeDeleteRefusedError,
    begin_scope_delete,
)

from .helpers import Embedder, config, runtime
from .test_provider import provider


def prepared_home(tmp_path, monkeypatch):
    """Initialize an embedded destination through the CLI and return parser and config."""
    from utils import atomic_json_write

    atomic_json_write(tmp_path / "qdrant-memory.json", config(), mode=0o600)
    monkeypatch.setattr("qdrant_memory.cli.build_embedder", lambda ctx, cfg: Embedder())
    parser = argparse.ArgumentParser()
    register_cli(parser)
    assert run(parser.parse_args(["init", "--existing", "use"]), home=tmp_path)["ok"]
    return parser, load_config(tmp_path)


def seed(cfg, records):
    """Insert payload fixtures directly into the embedded collection."""
    from qdrant_client import QdrantClient, models

    client = QdrantClient(path=cfg["qdrant"]["path"])
    try:
        client.upsert(
            cfg["qdrant"]["collection"],
            points=[
                models.PointStruct(id=i, vector={"dense": [1.0, 0.0, 0.0]}, payload=p)
                for i, p in records
            ],
        )
    finally:
        client.close()


def test_list_reports_scopes_and_ledger_backlog(tmp_path, monkeypatch):
    """List is a read-only inventory with scope filters, bounds and backlog counts."""
    parser, cfg = prepared_home(tmp_path, monkeypatch)
    seed(
        cfg,
        [
            (1, payload("alpha fact", Scope("u1", None), "manual", "s1")),
            (2, payload("beta fact", Scope("u1", "bot"), "manual", "s1")),
            (3, payload("gamma fact", Scope("u2", None), "manual", "s1")),
        ],
    )
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    ledger.enqueue_operation(
        "pending-point", "UPSERT", payload("pending", Scope("u1", None), "manual")
    )
    ledger.close()
    result = run(parser.parse_args(["list"]), home=tmp_path)
    assert result["count"] == {"total": 3, "returned": 3, "truncated": False}
    assert result["ledger"]["pending_operations"] == 1
    assert {m["text"] for m in result["memories"]} == {"alpha fact", "beta fact", "gamma fact"}
    limited = run(parser.parse_args(["list", "--limit", "1"]), home=tmp_path)
    assert limited["count"] == {"total": 3, "returned": 1, "truncated": True}
    scoped = run(parser.parse_args(["list", "--user", "u1", "--agent", "bot"]), home=tmp_path)
    assert scoped["count"]["total"] == 1
    assert scoped["memories"][0]["text"] == "beta fact"


def test_export_round_trips_through_mem0_import(tmp_path, monkeypatch):
    """Exports use the Mem0-importable shape and re-import into a fresh home."""
    parser, cfg = prepared_home(tmp_path, monkeypatch)
    seed(
        cfg,
        [
            (1, payload("alpha fact", Scope("u1", None), "manual", "s1")),
            (2, payload("beta fact", Scope("u2", "bot"), "manual", "s1")),
        ],
    )
    target = tmp_path / "export.json"
    result = run(parser.parse_args(["export", "--output", str(target)]), home=tmp_path)
    assert result["count"] == 2
    document = json.loads(target.read_text())
    assert document["format"] == "qdrant-memory-export"
    assert document["count"] == 2
    assert {r["memory"] for r in document["memories"]} == {"alpha fact", "beta fact"}
    record = next(r for r in document["memories"] if r["memory"] == "beta fact")
    assert record["user_id"] == "u2" and record["agent_id"] == "bot"
    no_agent = next(r for r in document["memories"] if r["memory"] == "alpha fact")
    assert "agent_id" in no_agent and no_agent["agent_id"] is None
    with pytest.raises(ExportTargetExistsError):
        run(parser.parse_args(["export", "--output", str(target)]), home=tmp_path)
    forced = run(parser.parse_args(["export", "--output", str(target), "--force"]), home=tmp_path)
    assert forced["count"] == 2
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    fresh_parser, _ = prepared_home(fresh, monkeypatch)
    imported = run(
        fresh_parser.parse_args(["migrate", "mem0", "--source-json", str(target)]),
        home=fresh,
    )
    assert imported["processed"] == 2
    listed = run(fresh_parser.parse_args(["list"]), home=fresh)
    assert listed["count"]["total"] == 2
    # The no-agent scope survives the round trip instead of drifting into the
    # importer's default agent scope (the profile scope defaults to an agent).
    scoped = run(fresh_parser.parse_args(["list", "--user", "u1"]), home=fresh)
    assert scoped["count"]["total"] == 1
    assert scoped["memories"][0]["agent_id"] is None


def test_delete_all_is_durable_and_fences_prepared_writes(tmp_path, monkeypatch):
    """Scoped deletion removes one scope, fences stale writes and keeps the rest."""
    parser, cfg = prepared_home(tmp_path, monkeypatch)
    seed(
        cfg,
        [
            (1, payload("alpha fact", Scope("u1", None), "manual", "s1")),
            (2, payload("alpha two", Scope("u1", None), "manual", "s1")),
            (3, payload("gamma fact", Scope("u2", None), "manual", "s1")),
        ],
    )
    stale = payload(
        "alpha fact", Scope("u1", None), "manual", "s1", updated_at="2020-01-01T00:00:00Z"
    )
    never = payload(
        "never stored fact",
        Scope("u1", None),
        "manual",
        "s1",
        updated_at="2020-01-01T00:00:00Z",
    )
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    stale_key = ledger.enqueue_operation("stale-point", "UPSERT", stale)
    never_key = ledger.enqueue_operation("never-written-point", "UPSERT", never)
    ledger.close()
    with pytest.raises(DeleteConfirmationRequiredError):
        run(parser.parse_args(["delete-all", "--user", "u1"]), home=tmp_path)
    dry = run(parser.parse_args(["delete-all", "--user", "u1", "--dry-run"]), home=tmp_path)
    assert dry["would_delete"] == 2
    assert run(parser.parse_args(["list"]), home=tmp_path)["count"]["total"] == 3
    result = run(parser.parse_args(["delete-all", "--user", "u1", "--confirm"]), home=tmp_path)
    assert result["deleted"] == 2 and result["resumed"] is False
    remaining = run(parser.parse_args(["list"]), home=tmp_path)
    assert remaining["count"]["total"] == 1
    assert remaining["memories"][0]["text"] == "gamma fact"
    runtime_home = runtime(tmp_path, ledger_namespace=ledger_namespace(cfg))
    runtime_home.commit([stale_key, never_key])
    assert runtime_home.ledger.row("operations", stale_key)["status"] == "SUPERSEDED"
    assert runtime_home.ledger.row("operations", never_key)["status"] == "SUPERSEDED"
    assert runtime_home.store.count() == 0
    runtime_home.store.close()
    runtime_home.ledger.close()


def test_repeated_resume_does_not_duplicate_fences(tmp_path, monkeypatch):
    """An interrupted scoped deletion resumes idempotently without fence growth."""
    parser, cfg = prepared_home(tmp_path, monkeypatch)
    seed(
        cfg,
        [
            (1, payload("alpha fact", Scope("u1", None), "manual", "s1")),
            (2, payload("alpha two", Scope("u1", None), "manual", "s1")),
        ],
    )
    from qdrant_memory.qdrant_store import QdrantStore

    original = QdrantStore.delete_scope
    calls = {"count": 0}

    def fail_once(self, scope):
        """Interrupt the first deletion attempt after its fences are committed."""
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated interruption")
        return original(self, scope)

    monkeypatch.setattr(QdrantStore, "delete_scope", fail_once)
    with pytest.raises(RuntimeError):
        run(parser.parse_args(["delete-all", "--user", "u1", "--confirm"]), home=tmp_path)
    check = Ledger(tmp_path, ledger_namespace(cfg))
    source_id = check.delete_fence_source(Scope("u1", None))
    first = len(
        [r for r in check.rows("operations", status="COMMITTED") if r["source_id"] == source_id]
    )
    check.close()
    resumed = run(parser.parse_args(["delete-all", "--confirm"]), home=tmp_path)
    assert resumed["resumed"] is True
    assert resumed["deleted"] == 2
    check = Ledger(tmp_path, ledger_namespace(cfg))
    second = len(
        [r for r in check.rows("operations", status="COMMITTED") if r["source_id"] == source_id]
    )
    check.close()
    assert first == second == 2


def test_provider_startup_fails_closed_while_scope_delete_pending(tmp_path):
    """Provider startup refuses to replay while a scoped deletion is pending."""
    p = provider(tmp_path)
    namespace = p.ledger.collection
    p.shutdown()
    ledger = Ledger(tmp_path, namespace)
    begin_scope_delete(ledger, Scope("u1", None))
    ledger.close()
    with pytest.raises(ScopeDeleteRecoveryRequiredError):
        provider(tmp_path)


def test_pending_events_are_invalidated_by_scoped_delete(tmp_path, monkeypatch):
    """Delete-all also invalidates admitted-but-unprocessed turn events."""
    parser, cfg = prepared_home(tmp_path, monkeypatch)
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    stale_event = {
        "kind": "turn",
        "source": "conversation",
        "session_id": "s1",
        "scope": {"user_id": "u1", "agent_id": None},
        "user": "cats preferred",
        "assistant": "ack",
        "turn_number": 1,
    }
    stale_key = ledger.enqueue_event(stale_event)
    failed_key = ledger.enqueue_event({**stale_event, "turn_number": 2})
    ledger.failure("events", failed_key, RuntimeError("extraction failed"), terminal=True)
    other_key = ledger.enqueue_event(
        {
            "kind": "turn",
            "source": "conversation",
            "session_id": "s2",
            "scope": {"user_id": "u2", "agent_id": None},
            "user": "dogs preferred",
            "assistant": "ack",
            "turn_number": 1,
        }
    )
    ledger.close()
    result = run(parser.parse_args(["delete-all", "--user", "u1", "--confirm"]), home=tmp_path)
    assert result["superseded_events"] == 2
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    assert ledger.row("events", stale_key)["status"] == "SUPERSEDED"
    assert ledger.row("events", failed_key)["status"] == "SUPERSEDED"
    assert ledger.row("events", other_key)["status"] == "PENDING"
    ledger.close()
    # Retry must not requeue the invalidated failed event.
    run(parser.parse_args(["retry"]), home=tmp_path)
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    assert ledger.row("events", failed_key)["status"] == "SUPERSEDED"
    ledger.close()
    # A provider restart recovers only the surviving scope's event.
    p = provider(tmp_path)
    assert p.wait_idle()
    assert p.store.count(Scope("u1", None)) == 0
    assert p.store.count(Scope("u2", None)) == 1
    p.shutdown()


def test_delete_all_all_agents_covers_every_scope(tmp_path, monkeypatch):
    """--all-agents deletes every agent scope; strict mode reports the rest."""
    parser, cfg = prepared_home(tmp_path, monkeypatch)
    seed(
        cfg,
        [
            (1, payload("no agent fact", Scope("u1", None), "manual", "s1")),
            (2, payload("hermes fact", Scope("u1", "hermes"), "manual", "s1")),
            (3, payload("bot fact", Scope("u1", "bot"), "manual", "s1")),
            (4, payload("other user fact", Scope("u2", None), "manual", "s1")),
        ],
    )
    strict = run(parser.parse_args(["delete-all", "--user", "u1", "--confirm"]), home=tmp_path)
    assert strict["deleted"] == 1
    assert strict["other_agent_scopes"] == {"bot": 1, "hermes": 1}
    dry = run(
        parser.parse_args(["delete-all", "--user", "u1", "--all-agents", "--dry-run"]),
        home=tmp_path,
    )
    assert dry["would_delete"] == 2
    assert {item["agent_id"] for item in dry["scopes"]} == {"bot", "hermes"}
    result = run(
        parser.parse_args(["delete-all", "--user", "u1", "--all-agents", "--confirm"]),
        home=tmp_path,
    )
    assert result["deleted"] == 2
    assert "other_agent_scopes" not in result
    assert run(parser.parse_args(["list"]), home=tmp_path)["count"]["total"] == 1
    with pytest.raises(ScopeSelectionError):
        run(
            parser.parse_args(
                ["delete-all", "--user", "u1", "--agent", "bot", "--all-agents", "--confirm"]
            ),
            home=tmp_path,
        )
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    begin_scope_delete(ledger, {"user_id": "u2", "agent_id": ALL_AGENTS})
    ledger.close()
    resumed = run(parser.parse_args(["delete-all", "--confirm"]), home=tmp_path)
    assert resumed["resumed"] is True and resumed["deleted"] == 1
    assert run(parser.parse_args(["list"]), home=tmp_path)["count"]["total"] == 0


def test_large_scope_pages_export_and_delete(tmp_path, monkeypatch):
    """Multi-page scopes stream through export and delete-all unchanged."""
    parser, cfg = prepared_home(tmp_path, monkeypatch)
    seed(
        cfg,
        [
            (1000 + index, payload(f"bulk fact {index}", Scope("u1", None), "manual", "s1"))
            for index in range(700)
        ],
    )
    target = tmp_path / "bulk.json"
    exported = run(parser.parse_args(["export", "--output", str(target)]), home=tmp_path)
    assert exported["count"] == 700
    document = json.loads(target.read_text())
    assert document["count"] == 700
    assert len(document["memories"]) == 700
    result = run(parser.parse_args(["delete-all", "--user", "u1", "--confirm"]), home=tmp_path)
    assert result["deleted"] == 700
    assert run(parser.parse_args(["list"]), home=tmp_path)["count"]["total"] == 0


def test_resume_restores_recorded_agent_scope(tmp_path, monkeypatch):
    """Resuming without scope arguments restores the recorded agent scope."""
    parser, cfg = prepared_home(tmp_path, monkeypatch)
    seed(
        cfg,
        [
            (1, payload("bot fact", Scope("u1", "bot"), "manual", "s1")),
            (2, payload("no agent fact", Scope("u1", None), "manual", "s1")),
        ],
    )
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    begin_scope_delete(ledger, Scope("u1", "bot"))
    ledger.close()
    resumed = run(parser.parse_args(["delete-all", "--confirm"]), home=tmp_path)
    assert resumed["resumed"] is True
    assert resumed["deleted"] == 1
    assert resumed["scope"] == {"user_id": "u1", "agent_id": "bot"}
    remaining = run(parser.parse_args(["list"]), home=tmp_path)
    assert remaining["count"]["total"] == 1
    assert remaining["memories"][0]["text"] == "no agent fact"
    assert run(parser.parse_args(["stats"]), home=tmp_path)["points_count"] == 1


def test_export_falls_back_without_hard_links(tmp_path, monkeypatch):
    """Filesystems without hard links fall back to exclusive create-and-copy."""
    parser, cfg = prepared_home(tmp_path, monkeypatch)
    seed(cfg, [(1, payload("alpha fact", Scope("u1", None), "manual", "s1"))])

    def unsupported(*args, **kwargs):
        """Simulate a filesystem that refuses hard links."""
        raise OSError("hard links unsupported")

    monkeypatch.setattr("qdrant_memory.inventory.os.link", unsupported)
    target = tmp_path / "fallback.json"
    result = run(parser.parse_args(["export", "--output", str(target)]), home=tmp_path)
    assert result["count"] == 1
    document = json.loads(target.read_text())
    assert document["count"] == 1 and document["memories"][0]["memory"] == "alpha fact"
    with pytest.raises(ExportTargetExistsError):
        run(parser.parse_args(["export", "--output", str(target)]), home=tmp_path)


def test_resume_refuses_mismatched_explicit_scope_flags(tmp_path, monkeypatch):
    """Resuming with explicit scope flags that contradict the intent is refused."""
    parser, cfg = prepared_home(tmp_path, monkeypatch)
    seed(cfg, [(1, payload("bot fact", Scope("u1", "bot"), "manual", "s1"))])
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    begin_scope_delete(ledger, Scope("u1", "bot"))
    ledger.close()
    with pytest.raises(ScopeDeleteRefusedError):
        run(parser.parse_args(["delete-all", "--all-agents", "--confirm"]), home=tmp_path)
    with pytest.raises(ScopeDeleteRefusedError):
        run(parser.parse_args(["delete-all", "--user", "u2", "--confirm"]), home=tmp_path)
    resumed = run(
        parser.parse_args(["delete-all", "--user", "u1", "--agent", "bot", "--confirm"]),
        home=tmp_path,
    )
    assert resumed["resumed"] is True and resumed["deleted"] == 1


def test_interrupted_scope_delete_blocks_commands_and_resumes(tmp_path, monkeypatch):
    """A recorded scoped-deletion intent fails closed until delete-all resumes it."""
    parser, cfg = prepared_home(tmp_path, monkeypatch)
    seed(cfg, [(1, payload("alpha fact", Scope("u1", None), "manual", "s1"))])
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    begin_scope_delete(ledger, Scope("u1", None))
    ledger.close()
    with pytest.raises(ScopeDeleteRecoveryRequiredError):
        run(parser.parse_args(["stats"]), home=tmp_path)
    with pytest.raises(ScopeDeleteRefusedError):
        run(parser.parse_args(["delete-all", "--user", "u2", "--confirm"]), home=tmp_path)
    resumed = run(parser.parse_args(["delete-all", "--confirm"]), home=tmp_path)
    assert resumed["resumed"] is True and resumed["deleted"] == 1
    assert run(parser.parse_args(["stats"]), home=tmp_path)["points_count"] == 0
    assert run(parser.parse_args(["list"]), home=tmp_path)["count"]["total"] == 0


def test_scope_delete_error_codes(tmp_path, monkeypatch, capsys):
    """CLI maps inventory and scope-deletion failures to stable sanitized codes."""
    from utils import atomic_json_write

    atomic_json_write(tmp_path / "qdrant-memory.json", config(), mode=0o600)
    monkeypatch.setattr("qdrant_memory.cli.active_home", lambda: tmp_path)
    monkeypatch.setattr("qdrant_memory.cli.build_embedder", lambda ctx, cfg: Embedder())
    parser = argparse.ArgumentParser()
    register_cli(parser)
    assert main(parser.parse_args(["init", "--existing", "use"])) == 0
    capsys.readouterr()
    assert main(parser.parse_args(["delete-all", "--user", "u1"])) == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "delete_confirmation_required"
    assert error["retryable"] is False
    assert main(parser.parse_args(["delete-all"])) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "scope_delete_refused"
    assert main(parser.parse_args(["list", "--agent", "bot"])) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "scope_selection_error"
    assert main(parser.parse_args(["delete-all", "--all-agents"])) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "scope_selection_error"
    assert (
        main(parser.parse_args(["delete-all", "--user", "u1", "--agent", "bot", "--all-agents"]))
        == 1
    )
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "scope_selection_error"
    target = tmp_path / "out.json"
    target.write_text("{}")
    assert main(parser.parse_args(["export", "--output", str(target)])) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "export_target_exists"
