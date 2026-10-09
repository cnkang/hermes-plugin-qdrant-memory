"""Memory inventory, portable export and scoped deletion contracts."""

import argparse
import json

import pytest

from qdrant_memory.cli import (
    DeleteConfirmationRequiredError,
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
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    stale_key = ledger.enqueue_operation("stale-point", "UPSERT", stale)
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
    runtime_home.commit([stale_key])
    row = runtime_home.ledger.row("operations", stale_key)
    assert row["status"] == "SUPERSEDED"
    assert runtime_home.store.count() == 0
    runtime_home.store.close()
    runtime_home.ledger.close()


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
    target = tmp_path / "out.json"
    target.write_text("{}")
    assert main(parser.parse_args(["export", "--output", str(target)])) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "export_target_exists"
