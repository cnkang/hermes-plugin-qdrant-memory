"""Private SQLite write-ahead ledger. Pending payloads survive process death."""

import json
import os
import sqlite3
from pathlib import Path
from threading import RLock

from .models import digest, now
from .retry import safe_error


class Ledger:
    """Persist replayable events, mutations, migration manifests and bounded metrics."""

    def __init__(self, home, collection="hermes_qdrant_memory"):
        """Open a private profile ledger with WAL and FULL synchronization."""
        self.collection = collection
        directory = Path(home) / "qdrant-memory"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        path = directory / "state.db"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        path.chmod(0o600)
        self.lock = RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, session_id TEXT, payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT, created_at TEXT, committed_at TEXT, collection TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations (
                idempotency_key TEXT PRIMARY KEY, point_id TEXT NOT NULL, action TEXT NOT NULL,
                payload_json TEXT NOT NULL, source_id TEXT, source_version TEXT, content_hash TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT, created_at TEXT, committed_at TEXT, collection TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS manifests (
                migration_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, collection TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metrics (name TEXT, value REAL, collection TEXT);
            CREATE TABLE IF NOT EXISTS counters (
                name TEXT, value INTEGER, collection TEXT, PRIMARY KEY(name,collection));
        """)

    def enqueue_event(self, value):
        """Persist an immutable event once and return its deterministic key."""
        key = digest([self.collection, value])
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO events(event_id,session_id,payload_json,created_at,collection) VALUES(?,?,?,?,?)",
                (
                    key,
                    value["session_id"],
                    json.dumps(value, ensure_ascii=False),
                    now(),
                    self.collection,
                ),
            )
        return key

    def enqueue_operation(
        self, identifier, action, value, source_id="", source_version="", generation=""
    ):
        # Timestamps are part of migration metadata updates; runtime replay uses
        # the same prepared operation persisted before the remote mutation.
        """Persist a prepared mutation and return its destination-scoped operation key.

        A nonempty generation creates fresh migration repair work even when identical
        payloads were committed earlier; the original source ID/version are retained.
        """
        identity = [self.collection, identifier, action, value, source_id, source_version]
        if generation:
            identity.append(generation)
        key = digest(identity)
        with self.lock, self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO operations
                (idempotency_key,point_id,action,payload_json,source_id,source_version,content_hash,created_at,collection)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    key,
                    identifier,
                    action,
                    json.dumps(value, ensure_ascii=False),
                    source_id,
                    source_version,
                    value.get("content_hash", ""),
                    now(),
                    self.collection,
                ),
            )
        return key

    def has_open_operations(self, identifier):
        """Check open writes to this point in this destination."""
        with self.lock:
            return (
                self.db.execute(
                    """SELECT 1 FROM operations WHERE point_id=? AND collection=?
                AND status IN ('PENDING','FAILED') LIMIT 1""",
                    (identifier, self.collection),
                ).fetchone()
                is not None
            )

    def rows(self, table, status="PENDING"):
        """Return destination-scoped rows in insertion order for the requested status."""
        if table not in {"events", "operations"}:
            raise ValueError("Unknown ledger table")
        with self.lock:
            return [
                dict(row)
                for row in self.db.execute(
                    f"SELECT * FROM {table} WHERE status=? AND collection=? ORDER BY rowid",
                    (status, self.collection),
                )
            ]

    def row(self, table, key):
        """Return an event/operation by key, or None when it is missing."""
        column = {"events": "event_id", "operations": "idempotency_key"}[table]
        with self.lock:
            row = self.db.execute(f"SELECT * FROM {table} WHERE {column}=?", (key,)).fetchone()
            return dict(row) if row else None

    def prepare_event(self, key, value):
        """Persist extraction or preparation progress before any service mutation."""
        with self.lock, self.db:
            self.db.execute(
                "UPDATE events SET payload_json=? WHERE event_id=?",
                (json.dumps(value, ensure_ascii=False), key),
            )

    def finish(self, table, key, status="COMMITTED"):
        """Acknowledge completion and supersede older open writes to the same point."""
        column = {"events": "event_id", "operations": "idempotency_key"}[table]
        with self.lock, self.db:
            self.db.execute(
                f"UPDATE {table} SET status=?,last_error=NULL,committed_at=? WHERE {column}=?",
                (status, now(), key),
            )
            if table == "operations" and status == "COMMITTED":
                # An older failed write must never overwrite a later committed update.
                self.db.execute(
                    """UPDATE operations SET status='SUPERSEDED' WHERE point_id=
                    (SELECT point_id FROM operations WHERE idempotency_key=?) AND rowid <
                    (SELECT rowid FROM operations WHERE idempotency_key=?) AND collection=? AND status IN ('PENDING','FAILED')""",
                    (key, key, self.collection),
                )

    def failure(self, table, key, exc, terminal=False, count_attempt=True):
        """Record a sanitized failure and optionally increment its attempt count."""
        column = {"events": "event_id", "operations": "idempotency_key"}[table]
        with self.lock, self.db:
            self.db.execute(
                f"UPDATE {table} SET attempts=attempts+?,last_error=?,status=? WHERE {column}=?",
                (
                    int(count_attempt),
                    json.dumps(safe_error(exc)),
                    "FAILED" if terminal else "PENDING",
                    key,
                ),
            )

    def retry_failed(self):
        """Requeue failed events and operations only for this destination."""
        with self.lock, self.db:
            for table in ("events", "operations"):
                self.db.execute(
                    f"UPDATE {table} SET status='PENDING' WHERE status='FAILED' AND collection=?",
                    (self.collection,),
                )

    def reset_operation(self, key):
        """Requeue one failed operation without touching other migration work."""
        with self.lock, self.db:
            self.db.execute(
                "UPDATE operations SET status='PENDING' WHERE idempotency_key=? AND status='FAILED'",
                (key,),
            )

    def stats(self):
        """Count destination-specific event states, operation states and attempts."""
        with self.lock:
            result = {
                table: dict(
                    self.db.execute(
                        f"SELECT status,COUNT(*) FROM {table} WHERE collection=? GROUP BY status",
                        (self.collection,),
                    )
                )
                for table in ("events", "operations")
            }
            result["retry_total"] = self.db.execute(
                "SELECT COALESCE(SUM(attempts),0) FROM operations WHERE collection=?",
                (self.collection,),
            ).fetchone()[0]
            result["dedupe"] = dict(
                self.db.execute(
                    "SELECT name,value FROM counters WHERE collection=?", (self.collection,)
                )
            )
            return result

    def increment(self, name):
        """Increment a durable destination-specific decision counter."""
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO counters VALUES(?,1,?) ON CONFLICT(name,collection) DO UPDATE SET value=value+1",
                (name, self.collection),
            )

    def save_manifest(self, value):
        """Durably store one destination-specific migration manifest."""
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO manifests VALUES(?,?,?)",
                (value["migration_id"], json.dumps(value), self.collection),
            )

    def manifests(self):
        """Read this destination's manifests in insertion order."""
        with self.lock:
            return [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT payload_json FROM manifests WHERE collection=? ORDER BY rowid",
                    (self.collection,),
                )
            ]

    def measure(self, name, value):
        """Record a metric sample, retaining at most 512 per name/destination."""
        with self.lock, self.db:
            self.db.execute("INSERT INTO metrics VALUES(?,?,?)", (name, value, self.collection))
            self.db.execute(
                """DELETE FROM metrics WHERE name=? AND collection=? AND rowid NOT IN
                (SELECT rowid FROM metrics WHERE name=? AND collection=? ORDER BY rowid DESC LIMIT 512)""",
                (name, self.collection, name, self.collection),
            )

    def metrics(self):
        """Summarize retained samples with counts and empirical p50/p95 values."""
        with self.lock:
            samples = list(
                self.db.execute(
                    "SELECT name,value FROM metrics WHERE collection=?", (self.collection,)
                )
            )
        groups = {}
        for name, value in samples:
            groups.setdefault(name, []).append(value)
        result = {}
        for name, values in groups.items():
            values.sort()
            result[name] = {
                "samples": len(values),
                "p50": values[(len(values) - 1) // 2],
                "p95": values[min(len(values) - 1, int(len(values) * 0.95))],
            }
        return result

    def close(self):
        """Close SQLite while excluding concurrent ledger access."""
        with self.lock:
            self.db.close()
