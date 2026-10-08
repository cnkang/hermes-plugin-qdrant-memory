"""Bounded ledger scans preserve order and isolate one finite recovery round."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from qdrant_memory.ledger import Ledger
from qdrant_memory.models import Scope, payload
from qdrant_memory.runtime import Runtime

from .helpers import config, runtime


def seed(ledger, count, table="operations"):
    """Insert synthetic private-free fixtures in one transaction."""
    value = json.dumps(payload("cats", Scope("test", None), "fixture", ""))
    with ledger.db:
        if table == "operations":
            ledger.db.executemany(
                "INSERT INTO operations(idempotency_key,point_id,action,payload_json,created_at,collection) "
                "VALUES(?,?,'UPSERT',?,'2020-01-01T00:00:00Z',?)",
                ((str(i), str(i), value, ledger.collection) for i in range(count)),
            )
        else:
            ledger.db.executemany(
                "INSERT INTO events(event_id,payload_json,collection) VALUES(?,?,?)",
                ((str(i), "{}", ledger.collection) for i in range(count)),
            )


@pytest.mark.parametrize("count", [0, 1, 100, 1000, 10000, 100000])
@pytest.mark.parametrize("table", ["operations", "events"])
def test_keyset_scan_is_bounded_and_stable(tmp_path, count, table):
    """Large backlogs use LIMIT, advance rowids, and release every query cursor."""
    ledger = Ledger(tmp_path)
    try:
        seed(ledger, count, table)
        queries = []
        ledger.db.set_trace_callback(lambda sql: queries.append(sql) if "LIMIT" in sql else None)
        seen = 0
        for row in ledger.iter_rows(table, batch_size=97):
            assert row["scan_rowid"] == seen + 1
            seen += 1
        assert seen == count
        assert all("OFFSET" not in sql and "LIMIT 97" in sql for sql in queries)
    finally:
        ledger.close()


def test_recovery_snapshot_status_changes_failure_and_new_work(tmp_path, monkeypatch):
    """Failures and concurrent status changes cannot skip following pages or extend a round."""
    ledger = Ledger(tmp_path)
    seed(ledger, 300)
    seed(ledger, 300, "events")
    rt = Runtime(config(), SimpleNamespace(), ledger)
    operations, events = [], []

    def commit(keys):
        """Observe real bounded recovery and change statuses at a batch boundary."""
        key = keys[0]
        operations.append(key)
        if key == "0":
            ledger.enqueue_operation("new", "DELETE", {})
            ledger.enqueue_event({"session_id": "new"})
            ledger.finish("operations", "150", "SUPERSEDED")
        if key == "129":
            raise ValueError("synthetic failure")
        ledger.finish("operations", key)

    monkeypatch.setattr(rt, "commit", commit)
    monkeypatch.setattr(rt, "process_event", lambda key: events.append(key))
    try:
        assert rt.recover() == 1
        assert operations == [str(i) for i in range(300) if i != 150]
        assert events == [str(i) for i in range(300)]
        assert ledger.row("operations", "129")["status"] == "PENDING"
        operations.clear()
        events.clear()
        assert rt.recover() == 1
        assert operations[0] == "129"
        assert len(operations) == 2
    finally:
        ledger.close()


@pytest.mark.parametrize("batch_size", [1, 2, 128])
def test_commit_rechecks_superseded_rows_between_batches(tmp_path, batch_size):
    """A later DELETE and an explicitly admitted re-add keep their order across batches."""
    rt = runtime(tmp_path, cfg=config(write={"batch_size": batch_size}))
    scope = Scope(**rt.cfg["scope"])
    value = payload("cats", scope, "fixture", "", updated_at="2020-01-01T00:00:00Z")
    identifier = "00000000-0000-0000-0000-000000000001"
    try:
        first = rt.operation(identifier, "UPSERT", value)
        delete = rt.operation(
            identifier,
            "DELETE",
            {"content_hash": value["content_hash"]},
            source_id=rt.ledger.delete_fence_source(scope),
        )
        fresh_value = payload("cats", scope, "fixture", "")
        last = rt.operation(identifier, "UPSERT", fresh_value)
        rt.commit([first, delete, last])
        assert rt.ledger.row("operations", first)["status"] == "SUPERSEDED"
        assert rt.ledger.row("operations", delete)["status"] == "COMMITTED"
        assert rt.ledger.row("operations", last)["status"] == "COMMITTED"
        assert rt.store.get(identifier, scope).payload == fresh_value
    finally:
        rt.store.close()
        rt.ledger.close()


def test_recovery_stop_between_batches_then_reopen_and_collection_isolation(tmp_path):
    """An owner can stop and reopen without losing pending work or crossing destinations."""
    ledger = Ledger(tmp_path, "one")
    other = Ledger(tmp_path, "two")
    seed(ledger, 300)
    # Distinct global keys, shared SQLite file.
    other_key = other.enqueue_operation("other", "DELETE", {})
    calls = []
    rt = Runtime(config(), SimpleNamespace(), ledger, stop_requested=lambda: len(calls) == 128)
    rt.commit = lambda keys: (calls.extend(keys), ledger.finish("operations", keys[0]))
    assert rt.recover() == 0
    ledger.close()
    ledger = Ledger(tmp_path, "one")
    try:
        assert len(list(ledger.iter_rows("operations"))) == 172
        assert other.row("operations", other_key)["status"] == "PENDING"
        ledger.failure("operations", "128", ValueError(), terminal=True)
        ledger.retry_failed()
        assert next(ledger.iter_rows("operations"))["idempotency_key"] == "128"
    finally:
        ledger.close()
        other.close()


def test_superseded_same_point_payloads_scrub_across_pages(tmp_path):
    """Acknowledging one write scrubs all older superseded bodies in bounded pages."""
    ledger = Ledger(tmp_path)
    try:
        keys = [ledger.enqueue_operation("same", "UPSERT", {"private": str(i)}) for i in range(300)]
        ledger.finish("operations", keys[-1])
        for key in keys:
            assert ledger.row("operations", key)["payload_json"] == "{}"
        assert ledger.stats()["operations"] == {"COMMITTED": 1, "SUPERSEDED": 299}
    finally:
        ledger.close()


def test_hard_exit_between_recovery_batches(tmp_path):
    """A real process death after one page preserves the next page for restart."""
    ledger = Ledger(tmp_path)
    seed(ledger, 300)
    ledger.close()
    code = """
import os, sys
from types import SimpleNamespace
from tests.helpers import config
from qdrant_memory.ledger import Ledger
from qdrant_memory.runtime import Runtime
ledger = Ledger(sys.argv[1])
store = SimpleNamespace(upsert=lambda rows: None, delete=lambda ids: None)
rt = Runtime(config(), store, ledger)
finish = ledger.finish
count = 0
def crash(table, key, status="COMMITTED"):
    global count
    finish(table, key, status)
    count += 1
    if count == 128:
        os._exit(74)
ledger.finish = crash
rt.recover()
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        capture_output=True,
        cwd=Path(__file__).resolve().parents[1],
        timeout=30,
    )
    assert result.returncode == 74, result.stderr.decode()
    ledger = Ledger(tmp_path)
    try:
        assert ledger.stats()["operations"] == {"COMMITTED": 128, "PENDING": 172}
        rt = Runtime(config(), SimpleNamespace(upsert=lambda rows: None), ledger)
        assert rt.recover() == 0
        assert ledger.stats()["operations"] == {"COMMITTED": 300}
    finally:
        ledger.close()


def test_network_phase_does_not_hold_ledger_lock(tmp_path):
    """Admission from another thread remains available during a remote service call."""
    from concurrent.futures import ThreadPoolExecutor

    ledger = Ledger(tmp_path)
    seed(ledger, 2)
    with ThreadPoolExecutor(max_workers=1) as pool:

        def network(_rows):
            """Use another thread to prove no SQLite ledger lock crosses this call."""
            future = pool.submit(ledger.enqueue_event, {"session_id": "admitted"})
            assert future.result(timeout=2)

        rt = Runtime(config(), SimpleNamespace(upsert=network), ledger)
        try:
            assert rt.recover() == 0
            # This new event belongs to the next recovery snapshot.
            assert ledger.stats()["events"] == {"PENDING": 1}
        finally:
            ledger.close()
