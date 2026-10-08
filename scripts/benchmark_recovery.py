"""Measure replay allocation, latency and retained SQLite storage on synthetic data.

Run with a Python environment containing the plugin and Hermes dependencies.
Every database lives in a TemporaryDirectory; no configured profile is opened.
"""

import argparse
import gc
import json
import platform
import sqlite3
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qdrant_memory.config import DEFAULTS
from qdrant_memory.ledger import Ledger
from qdrant_memory.models import Scope, payload
from qdrant_memory.runtime import Runtime

COLLECTION = "synthetic-recovery-benchmark"


class FakeStore:
    """Acknowledge writes without retaining points, embedding, or network access."""

    def __init__(self):
        """Initialize only constant-sized counters."""
        self.applied = 0

    def upsert(self, records):
        """Count confirmed synthetic UPSERTs without retaining their payloads."""
        self.applied += len(records)

    def delete(self, identifiers):
        """Count confirmed synthetic DELETEs without retaining identifiers."""
        self.applied += len(identifiers)


def measure(function):
    """Measure Python allocations, not RSS, and include the entire call duration."""
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    try:
        result = function()
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return {"seconds": round(elapsed, 6), "python_peak_bytes": peak}, result


def storage(ledger):
    """Describe live files and allocated pages without checkpointing or vacuuming."""
    path = Path(ledger.db.execute("PRAGMA database_list").fetchone()[2])
    return {
        "database_bytes": path.stat().st_size,
        "wal_bytes": Path(str(path) + "-wal").stat().st_size,
        "shm_bytes": Path(str(path) + "-shm").stat().st_size,
        "page_count": ledger.db.execute("PRAGMA page_count").fetchone()[0],
        "freelist_count": ledger.db.execute("PRAGMA freelist_count").fetchone()[0],
    }


def seed(home, size, text_bytes):
    """Bulk-admit isolated work in one FULL-synchronous transaction."""
    ledger = Ledger(home, COLLECTION)
    value = payload("x" * text_bytes, Scope("benchmark-user", None), "synthetic")
    serialized = json.dumps(value)
    with ledger.db:
        ledger.db.executemany(
            """INSERT INTO operations
            (idempotency_key,point_id,action,payload_json,source_id,source_version,
             content_hash,created_at,collection) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                (
                    f"operation-{number}",
                    f"point-{number}",
                    "UPSERT",
                    serialized,
                    "synthetic",
                    value["updated_at"],
                    value["content_hash"],
                    value["created_at"],
                    COLLECTION,
                )
                for number in range(size)
            ),
        )
    return ledger, len(serialized.encode())


def query_plans(ledger):
    """Capture the actual SQLite planner's list and bounded scan choices."""
    statements = {
        "legacy": (
            "SELECT * FROM operations WHERE status=? AND collection=? ORDER BY rowid",
            ("PENDING", COLLECTION),
        ),
        "bounded": (
            "SELECT rowid AS scan_rowid,* FROM operations WHERE collection=? AND status=? "
            "AND rowid>? AND rowid<=? ORDER BY rowid LIMIT ?",
            (COLLECTION, "PENDING", 0, 100000, 128),
        ),
    }
    return {
        name: [row[3] for row in ledger.db.execute("EXPLAIN QUERY PLAN " + sql, args)]
        for name, (sql, args) in statements.items()
    }


def run_case(size, text_bytes):
    """Compare old materialization with bounded replay using equivalent databases."""
    with tempfile.TemporaryDirectory(prefix="qdrant-recovery-benchmark-") as directory:
        home = Path(directory)
        ledger, payload_bytes = seed(home / "bounded", size, text_bytes)
        result = {"pending": size, "payload_json_bytes_per_operation": payload_bytes}
        result["query_plans"] = query_plans(ledger)
        result["storage_before_recovery"] = storage(ledger)
        legacy_scan, count = measure(lambda: len(ledger.rows("operations")))
        assert count == size
        bounded_scan, count = measure(lambda: sum(1 for _ in ledger.iter_rows("operations")))
        assert count == size
        result["legacy_scan"] = legacy_scan
        result["bounded_scan"] = bounded_scan

        store = FakeStore()
        runtime = Runtime(DEFAULTS, store, ledger)
        result["bounded_recovery"], failures = measure(runtime.recover)
        assert failures == 0 and store.applied == size
        remaining, retained = ledger.db.execute(
            "SELECT SUM(status!='COMMITTED'),SUM(payload_json!='{}') FROM operations"
        ).fetchone()
        assert not remaining and not retained
        result["storage_after_recovery"] = storage(ledger)
        result["correctness"] = {
            "applied": store.applied,
            "failures": failures,
            "noncommitted": remaining or 0,
            "retained_terminal_payloads": retained or 0,
        }
        ledger.close()
        result["terminal_reopen"], reopened = measure(lambda: Ledger(home / "bounded", COLLECTION))
        result["storage_after_reopen"] = storage(reopened)
        reopened.close()

        legacy, _ = seed(home / "legacy", size, text_bytes)
        # Reproduce the former recovery reader while running the same actual
        # Runtime.recover/commit/finish code. No concurrent admissions occur here.
        legacy.iter_rows = lambda table, **_kwargs: iter(legacy.rows(table))
        legacy_store = FakeStore()
        result["legacy_reader_recovery"], failures = measure(
            Runtime(DEFAULTS, legacy_store, legacy).recover
        )
        assert failures == 0 and legacy_store.applied == size
        legacy.close()
        return result


def main():
    """Print reproducible per-case JSON measurements and environment metadata."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[0, 1, 100, 1000, 10000, 100000])
    parser.add_argument("--text-bytes", type=int, default=256)
    args = parser.parse_args()
    if min(args.sizes) < 0 or args.text_bytes < 1:
        parser.error("sizes must be nonnegative and text-bytes positive")
    print(
        json.dumps(
            {
                "environment": {
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                    "sqlite": sqlite3.sqlite_version,
                    "allocation_metric": "tracemalloc Python peak bytes, not RSS",
                    "durability": "WAL / synchronous=FULL; no per-operation durability bypass",
                    "fixture_admission": "one executemany transaction per synthetic database",
                    "store": "FakeStore; no Qdrant network or embeddings",
                }
            }
        ),
        flush=True,
    )
    for size in args.sizes:
        print(json.dumps(run_case(size, args.text_bytes)), flush=True)


if __name__ == "__main__":
    main()
