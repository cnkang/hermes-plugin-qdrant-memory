"""Migration completion must distinguish overwritten intent from applied writes."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from qdrant_memory import cli
from qdrant_memory.migration import migrate, verify_manifest
from qdrant_memory.models import Scope

from .helpers import config, runtime


@pytest.mark.parametrize("timestamp", ["2000-01-01T00:00:00Z", "2099-01-01T00:00:00Z"])
@pytest.mark.parametrize("same_point", [True, False])
def test_superseded_resume_is_terminal_and_new_migration_is_authorized(
    tmp_path, monkeypatch, timestamp, same_point
):
    """Old prepared imports cannot resurrect deletes; a new import may re-add."""
    rt = runtime(tmp_path)
    records = [{"id": "one", "memory": "cats", "updated_at": timestamp}]
    original = rt.store.upsert
    try:
        monkeypatch.setattr(
            rt.store, "upsert", lambda _records: (_ for _ in ()).throw(ValueError())
        )
        with pytest.raises(ValueError):
            migrate(rt, records, "mem0-json", "fixture")
        manifest = rt.ledger.manifests()[-1]
        identifier, record = next(iter(manifest["records"].items()))
        old_key = record["operation_key"]
        monkeypatch.setattr(rt.store, "upsert", original)
        deleted_id = identifier if same_point else "00000000-0000-0000-0000-000000000001"
        # Another source can introduce the same content at another point.
        value = json.loads(rt.ledger.row("operations", old_key)["payload_json"])
        rt.store.upsert([(deleted_id, value)])
        key = rt.operation(
            deleted_id,
            "DELETE",
            {"content_hash": value["content_hash"]},
            source_id=rt.ledger.delete_fence_source(Scope(**rt.cfg["scope"])),
        )
        rt.commit([key])
        resumed = migrate(rt, records, "mem0-json", "fixture", resume=True, retry_failed=True)
        assert resumed["migration_id"] == manifest["migration_id"]
        assert resumed["completed_at"] is not None
        assert resumed["processed"] == 1
        assert resumed["superseded"] == 1
        assert resumed["applied"] == resumed["added"] == resumed["updated"] == 0
        assert rt.ledger.row("operations", old_key)["status"] == "SUPERSEDED"
        assert rt.store.get(identifier, Scope(**rt.cfg["scope"])) is None
        verification = verify_manifest(rt.store, resumed)
        assert not verification["ok"]
        assert verification["superseded"] == [identifier]
        again = migrate(rt, records, "mem0-json", "fixture", resume=True, retry_failed=True)
        assert again["migration_id"] == resumed["migration_id"]
        fresh = migrate(rt, records, "mem0-json", "fixture", verify=True)
        assert fresh["migration_id"] != resumed["migration_id"]
        assert fresh["applied"] == 1
    finally:
        rt.store.close()
        rt.ledger.close()


@pytest.mark.parametrize("skip", [False, True])
def test_completed_old_schema_resume_preserves_delete_and_cli_conflict(tmp_path, skip):
    """Legacy completed manifests and SKIPs cannot authorize resurrection on resume."""
    rt = runtime(tmp_path)
    records = [{"id": "one", "memory": "cats"}]
    scope = Scope(**rt.cfg["scope"])
    try:
        manifest = migrate(rt, records, "mem0-json", "fixture")
        if skip:
            manifest = migrate(rt, records, "mem0-json", "fixture")
        identifier, record = next(iter(manifest["records"].items()))
        record.pop("content_hash")
        record.pop("status")
        manifest.pop("superseded")
        manifest.pop("applied")
        rt.ledger.save_manifest(manifest)
        value = rt.store.get(identifier, scope).payload
        delete = rt.operation(
            identifier,
            "DELETE",
            {"content_hash": value["content_hash"]},
            source_id=rt.ledger.delete_fence_source(scope),
        )
        rt.commit([delete])
        args = argparse.Namespace(
            qdrant_command="migrate", resume=True, retry_failed=True, verify=False
        )
        result = cli.execute_command(args, rt, (records, "mem0-json", "fixture", None))
        assert result["superseded"] == result["processed"] == 1
        assert result["applied"] == (0 if skip else 1)
        assert result["skipped"] == int(skip)
        assert result["added"] == int(not skip)
        assert result["migration_id"] == manifest["migration_id"]
        assert not cli.verify_target(rt.store, rt.ledger)["ok"]
        args.verify = True
        with pytest.raises(ValueError, match="superseded"):
            cli.execute_command(args, rt, (records, "mem0-json", "fixture", None))
        assert rt.store.count() == 0
    finally:
        rt.store.close()
        rt.ledger.close()


def test_mixed_manifest_failed_delete_and_scope_isolation(tmp_path, monkeypatch):
    """SKIP/UPDATE/ADD counts remain accurate with partial cancellation and failed DELETE."""
    rt = runtime(tmp_path)
    original = rt.store.upsert
    try:
        initial = [{"id": "skip", "memory": "cats"}, {"id": "update", "memory": "dogs"}]
        migrate(rt, initial, "mem0-json", "initial")
        records = [
            initial[0],
            {"id": "update", "memory": "cats changed"},
            {"id": "add", "memory": "birds"},
            {"id": "other", "memory": "birds", "user_id": "other"},
        ]
        monkeypatch.setattr(rt.store, "upsert", lambda _r: (_ for _ in ()).throw(ValueError()))
        with pytest.raises(ValueError):
            migrate(rt, records, "mem0-json", "mixed")
        manifest = rt.ledger.manifests()[-1]
        add_id = next(
            i
            for i, r in manifest["records"].items()
            if r["action"] == "ADD" and r["scope"]["user_id"] != "other"
        )
        add = manifest["records"][add_id]
        value = json.loads(rt.ledger.row("operations", add["operation_key"])["payload_json"])
        delete = rt.operation(
            add_id,
            "DELETE",
            {"content_hash": value["content_hash"]},
            source_id=rt.ledger.delete_fence_source(Scope(**add["scope"])),
        )
        original_delete = rt.store.delete
        monkeypatch.setattr(rt.store, "delete", lambda _r: (_ for _ in ()).throw(ValueError()))
        with pytest.raises(ValueError):
            rt.commit([delete])
        monkeypatch.setattr(rt.store, "upsert", original)
        resumed = migrate(rt, records, "mem0-json", "mixed", resume=True, retry_failed=True)
        assert (
            resumed["processed"],
            resumed["applied"],
            resumed["superseded"],
            resumed["skipped"],
            resumed["added"],
            resumed["updated"],
        ) == (4, 2, 1, 1, 1, 1)
        assert resumed["completed_at"]
        monkeypatch.setattr(rt.store, "delete", original_delete)
        rt.ledger.retry_failed()
        rt.recover()
        assert rt.store.get(add_id, Scope(**add["scope"])) is None
        changed = [{"id": "add", "memory": "birds revised"}]
        new = migrate(rt, changed, "mem0-json", "mixed", resume=True, verify=True)
        assert new["migration_id"] != resumed["migration_id"]
        assert new["applied"] == 1
    finally:
        rt.store.close()
        rt.ledger.close()


def test_migration_hard_exit_then_resume(tmp_path):
    """Persisted prepared imports survive abrupt death before Qdrant writes."""
    code = """
import os, sys
from qdrant_client import QdrantClient
from qdrant_memory.migration import migrate
from tests.helpers import runtime
rt = runtime(sys.argv[1], client=QdrantClient(path=sys.argv[2]))
rt.store.upsert = lambda records: os._exit(73)
migrate(rt, [{"id":"one","memory":"cats"}], "mem0-json", "crash")
"""
    target = tmp_path / "target"
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), str(target)],
        capture_output=True,
        cwd=Path(__file__).resolve().parents[1],
        timeout=30,
    )
    assert result.returncode == 73, result.stderr.decode()
    rt = runtime(tmp_path, client=QdrantClient(path=str(target)), cfg=config())
    try:
        original = rt.ledger.manifests()[-1]
        resumed = migrate(
            rt,
            [{"id": "one", "memory": "cats"}],
            "mem0-json",
            "crash",
            resume=True,
            retry_failed=True,
            verify=True,
        )
        assert resumed["migration_id"] == original["migration_id"]
        assert resumed["applied"] == 1
        assert rt.store.count() == 1
        again = migrate(
            rt, [{"id": "one", "memory": "cats"}], "mem0-json", "crash", resume=True, verify=True
        )
        assert again["migration_id"] == resumed["migration_id"]
        assert rt.store.count() == 1
    finally:
        rt.store.close()
        rt.ledger.close()


def test_main_reports_safe_superseded_conflict(tmp_path, monkeypatch, capsys):
    """The public CLI preserves actionable conflict information without source text."""
    rt = runtime(tmp_path)
    records = [{"id": "private-source-id", "memory": "private-memory-payload"}]
    scope = Scope(**rt.cfg["scope"])
    try:
        manifest = migrate(rt, records, "mem0-json", "private-source-path")
        identifier = next(iter(manifest["records"]))
        value = rt.store.get(identifier, scope).payload
        key = rt.operation(
            identifier,
            "DELETE",
            {"content_hash": value["content_hash"]},
            source_id=rt.ledger.delete_fence_source(scope),
        )
        rt.commit([key])
        args = argparse.Namespace(
            qdrant_command="migrate", resume=True, retry_failed=True, verify=True
        )
        monkeypatch.setattr(
            cli,
            "run",
            lambda args: cli.execute_command(
                args, rt, (records, "mem0-json", "private-source-path", None)
            ),
        )
        assert cli.main(args) == 1
        output = capsys.readouterr().out
        error = json.loads(output)["error"]
        assert error["code"] == "migration_superseded"
        assert error["retryable"] is False
        assert "Inspect the manifest" in error["message"]
        assert "will not restore deleted memories" in error["message"]
        assert "without --resume" in error["message"]
        assert "private-" not in output
    finally:
        rt.store.close()
        rt.ledger.close()
