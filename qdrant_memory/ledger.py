"""Private SQLite write-ahead ledger. Pending payloads survive process death."""

import json
import os
import sqlite3
from datetime import datetime
from itertools import chain
from pathlib import Path
from threading import RLock

from .models import digest, now, timestamp
from .retry import safe_error

DELETE_FENCE_PREFIX = "hermes-qdrant-delete-fence-v1:"


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
            CREATE INDEX IF NOT EXISTS events_by_collection_status
                ON events(collection,status);
            CREATE INDEX IF NOT EXISTS operations_by_collection_status
                ON operations(collection,status);
            CREATE INDEX IF NOT EXISTS operations_by_point_status
                ON operations(collection,point_id,status);
            CREATE INDEX IF NOT EXISTS operations_delete_by_point
                ON operations(collection,source_id,action,point_id);
            CREATE INDEX IF NOT EXISTS operations_delete_by_hash
                ON operations(collection,source_id,action,content_hash);
            CREATE INDEX IF NOT EXISTS operations_unscrubbed_by_collection
                ON operations(collection)
                WHERE status IN ('COMMITTED','SUPERSEDED') AND payload_json!='{}';
            CREATE INDEX IF NOT EXISTS operations_unscrubbed_by_point
                ON operations(collection,point_id)
                WHERE status IN ('COMMITTED','SUPERSEDED') AND payload_json!='{}';
            CREATE TABLE IF NOT EXISTS manifests (
                migration_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, collection TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metrics (name TEXT, value REAL, collection TEXT);
            CREATE TABLE IF NOT EXISTS counters (
                name TEXT, value INTEGER, collection TEXT, PRIMARY KEY(name,collection));
            CREATE TABLE IF NOT EXISTS unattributed_sessions (
                session_id TEXT, collection TEXT, PRIMARY KEY(session_id,collection));
        """)
        self._resumable_operation_keys = self._load_resumable_operation_keys()
        with self.lock, self.db:
            self._scrub_terminal_payloads()

    def clear_destination(self):
        """Forget only this destination's work so cleared memories cannot replay.

        Transcript provenance quarantine remains in place across collection resets.
        """
        with self.lock, self.db:
            for table in ("events", "operations", "manifests", "metrics", "counters"):
                self.db.execute(f"DELETE FROM {table} WHERE collection=?", (self.collection,))
            self._resumable_operation_keys = set()

    def unattributed_sessions(self):
        """Load transcript quarantine for this destination, including resumed sessions."""
        with self.lock:
            return {
                row[0]
                for row in self.db.execute(
                    "SELECT session_id FROM unattributed_sessions WHERE collection=?",
                    (self.collection,),
                )
            }

    def set_session_unattributed(self, session_id, unattributed=True):
        """Retain uncertain transcript provenance until the host explicitly resets it."""
        with self.lock, self.db:
            if unattributed:
                self.db.execute(
                    "INSERT OR IGNORE INTO unattributed_sessions VALUES(?,?)",
                    (session_id, self.collection),
                )
            else:
                self.db.execute(
                    "DELETE FROM unattributed_sessions WHERE session_id=? AND collection=?",
                    (session_id, self.collection),
                )

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

    @staticmethod
    def delete_fence_source(scope):
        """Return a scope-bound marker for deletes that fence older admitted events."""
        scope_value = scope.as_dict() if hasattr(scope, "as_dict") else dict(scope)
        return DELETE_FENCE_PREFIX + digest(scope_value)

    def is_delete_fenced(self, identifier, scope, admitted_at, candidate_hash=None):
        """Check whether a scoped delete supersedes an older point or content hash."""
        source_id = self.delete_fence_source(scope)
        with self.lock:
            queries = [
                self.db.execute(
                    """SELECT source_version,created_at FROM operations
                WHERE action='DELETE' AND source_id=? AND collection=?
                AND point_id=?
                ORDER BY rowid""",
                    (source_id, self.collection, identifier),
                )
            ]
            if candidate_hash:
                queries.append(
                    self.db.execute(
                        """SELECT source_version,created_at FROM operations
                        WHERE action='DELETE' AND source_id=? AND collection=?
                        AND content_hash=? ORDER BY rowid""",
                        (source_id, self.collection, candidate_hash),
                    )
                )
            try:
                for row in chain.from_iterable(queries):
                    if not admitted_at:
                        return True
                    try:
                        event_time = datetime.fromisoformat(
                            timestamp(admitted_at).replace("Z", "+00:00")
                        )
                        delete_time = row["source_version"] or row["created_at"]
                        if not delete_time:
                            return True
                        delete_at = datetime.fromisoformat(
                            timestamp(delete_time).replace("Z", "+00:00")
                        )
                        if event_time <= delete_at:
                            return True
                    except (TypeError, ValueError, OverflowError):
                        return True
                return False
            finally:
                for cursor in queries:
                    cursor.close()

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

    def high_watermark(self, table):
        """Snapshot a destination scan boundary before replay starts."""
        if table not in {"events", "operations"}:
            raise ValueError("Unknown ledger table")
        with self.lock:
            return self.db.execute(
                f"SELECT COALESCE(MAX(rowid),0) FROM {table} WHERE collection=?",
                (self.collection,),
            ).fetchone()[0]

    def iter_rows(self, table, status="PENDING", batch_size=128, upper=None):
        """Yield bounded keyset batches without keeping a cursor or lock across yields.

        The fixed upper rowid excludes concurrent admissions. State changes cannot
        shift pages, and every row is returned at most once per scan.
        """
        if table not in {"events", "operations"}:
            raise ValueError("Unknown ledger table")
        if batch_size < 1:
            raise ValueError("Batch size must be positive")
        upper = self.high_watermark(table) if upper is None else upper
        after = 0
        while after < upper:
            with self.lock:
                cursor = self.db.execute(
                    f"SELECT rowid AS scan_rowid,* FROM {table} "
                    "WHERE collection=? AND status=? AND rowid>? AND rowid<=? "
                    "ORDER BY rowid LIMIT ?",
                    (self.collection, status, after, upper, batch_size),
                )
                try:
                    batch = [dict(row) for row in cursor.fetchall()]
                finally:
                    cursor.close()
            if not batch:
                return
            after = batch[-1]["scan_rowid"]
            yield from batch

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
            operation_keys = [key] if table == "operations" else []
            self.db.execute(
                f"UPDATE {table} SET status=?,last_error=NULL,committed_at=? WHERE {column}=?",
                (status, now(), key),
            )
            if table == "operations" and status == "COMMITTED":
                # An older failed write must never overwrite a later committed update.
                current = self.db.execute(
                    "SELECT point_id,rowid FROM operations WHERE idempotency_key=? AND collection=?",
                    (key, self.collection),
                ).fetchone()
                self.db.execute(
                    """UPDATE operations SET status='SUPERSEDED' WHERE point_id=
                    (SELECT point_id FROM operations WHERE idempotency_key=?) AND rowid <
                    (SELECT rowid FROM operations WHERE idempotency_key=?) AND collection=? AND status IN ('PENDING','FAILED')""",
                    (key, key, self.collection),
                )
                if current:
                    operation_keys = chain(
                        operation_keys,
                        (
                            row["idempotency_key"]
                            for row in self._terminal_operation_rows(
                                current["point_id"], current["rowid"]
                            )
                        ),
                    )
            self._scrub_terminal_payloads(
                event_keys=[key] if table == "events" else [],
                operation_keys=operation_keys,
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
            self._resumable_operation_keys = self._load_resumable_operation_keys()
            self._scrub_terminal_payloads()

    def scrub_terminal_payloads(self):
        """Scrub completed event and operation bodies while retaining dedupe rows."""
        with self.lock, self.db:
            return self._scrub_terminal_payloads()

    def _load_resumable_operation_keys(self):
        """Find operations referenced by manifests that have not completed."""
        keys = set()
        for row in self.db.execute(
            "SELECT payload_json FROM manifests WHERE collection=?", (self.collection,)
        ):
            try:
                manifest = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                return None
            if not isinstance(manifest, dict):
                return None
            if manifest.get("completed_at") is not None:
                continue
            records = manifest.get("records", {})
            if not isinstance(records, dict):
                return None
            for record in records.values():
                if not isinstance(record, dict):
                    return None
                key = record.get("operation_key")
                if key:
                    keys.add(key)
        return keys

    def _scrub_terminal_payloads(self, event_keys=None, operation_keys=None):
        """Scrub terminal bodies except operations still needed by an open migration."""
        if event_keys is None:
            event_count = self.db.execute(
                """UPDATE events SET payload_json='{}'
                WHERE collection=? AND status IN ('COMMITTED','SUPERSEDED')
                AND payload_json!='{}'""",
                (self.collection,),
            ).rowcount
        else:
            event_count = sum(
                self.db.execute(
                    """UPDATE events SET payload_json='{}'
                    WHERE event_id=? AND collection=?
                    AND status IN ('COMMITTED','SUPERSEDED')
                    AND payload_json!='{}'""",
                    (key, self.collection),
                ).rowcount
                for key in event_keys
            )

        resumable_keys = self._resumable_operation_keys
        if resumable_keys is None:
            return {"events": event_count, "operations": 0}
        if operation_keys is None:
            rows = self._terminal_operation_rows()
        else:

            def selected_rows():
                """Read one acknowledgement at a time, including superseded history."""
                for key in operation_keys:
                    row = self.db.execute(
                        """SELECT idempotency_key,payload_json FROM operations
                    WHERE idempotency_key=? AND collection=?
                    AND status IN ('COMMITTED','SUPERSEDED')""",
                        (key, self.collection),
                    ).fetchone()
                    if row:
                        yield row

            rows = selected_rows()
        operation_count = 0
        for row in rows:
            key = row["idempotency_key"]
            if key in resumable_keys or row["payload_json"] == "{}":
                continue
            operation_count += self.db.execute(
                """UPDATE operations SET payload_json='{}'
                WHERE idempotency_key=? AND collection=?
                AND status IN ('COMMITTED','SUPERSEDED')""",
                (key, self.collection),
            ).rowcount
        return {"events": event_count, "operations": operation_count}

    def _terminal_operation_rows(self, point=None, before=None):
        """Read scrub candidates in stable pages, closing queries before mutations."""
        upper = self.high_watermark("operations") if before is None else before - 1
        after = 0
        while after < upper:
            index = (
                "operations_unscrubbed_by_collection"
                if point is None
                else "operations_unscrubbed_by_point"
            )
            sql = f"""SELECT rowid AS scan_rowid,idempotency_key,payload_json FROM operations INDEXED BY {index}
                WHERE collection=? AND status IN ('COMMITTED','SUPERSEDED')
                AND payload_json!='{{}}' AND rowid>? AND rowid<=?"""
            parameters = [self.collection, after, upper]
            if point is not None:
                sql += " AND point_id=?"
                parameters.append(point)
            with self.lock:
                cursor = self.db.execute(sql + " ORDER BY rowid LIMIT 128", parameters)
                try:
                    rows = cursor.fetchall()
                finally:
                    cursor.close()
            if not rows:
                return
            after = rows[-1]["scan_rowid"]
            yield from rows

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
