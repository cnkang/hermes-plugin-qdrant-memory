"""Private SQLite write-ahead ledger. Pending payloads survive process death."""

import json
import os
import sqlite3
from collections import Counter
from datetime import datetime
from itertools import chain, islice
from pathlib import Path
from threading import RLock

from .event_quarantine import record_json
from .models import digest, now, timestamp
from .retry import safe_error

DELETE_FENCE_PREFIX = "hermes-qdrant-delete-fence-v1:"
UNKNOWN_TABLE_MESSAGE = "Unknown ledger table"
OWNER_BACKLOG_PER_AUTHORITY = 128
OWNER_BACKLOG_TOTAL = 1024
_OWNER_AUTHORITY_BACKFILL = "owner_authority_v1"


class OwnerBacklogFull(RuntimeError):
    """The durable Owner lifecycle backlog cannot accept another event."""


def _manifest_record_keys(records):
    """Collect operation keys from one manifest record map, or None if malformed."""
    if not isinstance(records, dict):
        return None
    keys = set()
    for record in records.values():
        if not isinstance(record, dict):
            return None
        key = record.get("operation_key")
        if key:
            keys.add(key)
    return keys


def _manifest_operation_keys(payload_json):
    """Return operation keys an unfinished manifest retains, or None if unreadable."""
    try:
        manifest = json.loads(payload_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    if manifest.get("completed_at") is not None:
        return set()
    return _manifest_record_keys(manifest.get("records", {}))


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
        self.path = path
        self.lock = RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        try:
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY, session_id TEXT, payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT, created_at TEXT, committed_at TEXT, collection TEXT NOT NULL,
                    owner_authority TEXT NOT NULL DEFAULT '');
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
                CREATE TABLE IF NOT EXISTS session_provenance (
                    session_id TEXT, collection TEXT, authors_json TEXT NOT NULL,
                    explicit_author INTEGER NOT NULL, history_known INTEGER NOT NULL,
                    PRIMARY KEY(session_id,collection));
                CREATE TABLE IF NOT EXISTS ledger_backfills (
                    name TEXT NOT NULL, collection TEXT NOT NULL, completed_at TEXT NOT NULL,
                    PRIMARY KEY(name,collection));
            """)
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(events)")}
            if "owner_authority" not in columns:
                # Additive upgrade: existing rows keep an empty authority and
                # are no longer charged to every capture principal's budget.
                self.db.execute(
                    "ALTER TABLE events ADD COLUMN owner_authority TEXT NOT NULL DEFAULT ''"
                )
                self.db.commit()
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS events_by_collection_authority_status "
                "ON events(collection,owner_authority,status)"
            )
            self._backfill_event_authority()
            self._resumable_operation_keys = self._load_resumable_operation_keys()
            with self.lock, self.db:
                self._scrub_terminal_payloads()
        except BaseException as exc:
            try:
                self.db.close()
            except Exception as cleanup_exc:
                exc.add_note(f"SQLite ledger cleanup failed ({type(cleanup_exc).__name__})")
            raise

    def _backfill_event_authority(self):
        """Attribute unsettled lifecycle events written before the column existed.

        The scan runs once per collection, under the ledger lock and in the same
        transaction as the completion marker. An interruption before that commit
        leaves the marker absent, so the next open retries the idempotent scan.
        Rows whose payload is not a JSON object with a dict ``owner_authority``
        are permanently unattributable: they stay empty and do not hold the
        marker back.
        """
        with self.lock, self.db:
            completed = self.db.execute(
                "SELECT 1 FROM ledger_backfills WHERE name=? AND collection=?",
                (_OWNER_AUTHORITY_BACKFILL, self.collection),
            ).fetchone()
            if completed:
                return
            rows = self.db.execute(
                "SELECT event_id,payload_json FROM events WHERE collection=? "
                "AND owner_authority='' AND status IN ('PENDING','FAILED','QUARANTINED')",
                (self.collection,),
            )
            updates = []
            for row in rows:
                try:
                    value = json.loads(row["payload_json"])
                except (TypeError, ValueError):
                    continue
                authority = value.get("owner_authority") if isinstance(value, dict) else None
                if isinstance(authority, dict):
                    updates.append((digest(authority), row["event_id"]))
            if updates:
                self.db.executemany("UPDATE events SET owner_authority=? WHERE event_id=?", updates)
            self.db.execute(
                "INSERT OR REPLACE INTO ledger_backfills(name,collection,completed_at) VALUES(?,?,?)",
                (_OWNER_AUTHORITY_BACKFILL, self.collection, now()),
            )

    def clear_destination(self):
        """Forget every destination-scoped replay record so cleared memories cannot replay.

        Transcript authorship and quarantine remain in place across collection resets,
        but admission receipts and prepared shared plans hold raw input or prepared
        payloads: they are replay work too, and a retained receipt can re-create a
        cleared memory on the next Owner start.
        """
        with self.lock, self.db:
            tables = [
                "events",
                "operations",
                "manifests",
                "metrics",
                "counters",
                "owner_events_v1",
                "shared_requests_v1",
            ]
            for table in tables:
                if (
                    table in {"owner_events_v1", "shared_requests_v1"}
                    and not self.db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone()
                ):
                    continue
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

    def session_provenance(self, session_id=None):
        """Load durable transcript attribution, independently of replay payloads."""
        with self.lock:
            return {
                row["session_id"]: {
                    "authors": set(json.loads(row["authors_json"])),
                    "explicit_author": bool(row["explicit_author"]),
                    "history_known": bool(row["history_known"]),
                }
                for row in self.db.execute(
                    "SELECT * FROM session_provenance WHERE collection=?"
                    + (" AND session_id=?" if session_id is not None else ""),
                    (self.collection, session_id) if session_id is not None else (self.collection,),
                )
            }

    def _save_session_provenance(self, session_id, value):
        """Save attribution within the caller's locked transaction."""
        self.db.execute(
            "INSERT OR REPLACE INTO session_provenance VALUES(?,?,?,?,?)",
            (
                session_id,
                self.collection,
                json.dumps(sorted(value["authors"])),
                int(value["explicit_author"]),
                int(value["history_known"]),
            ),
        )
        return value

    def invalidate_transcript_history(self):
        """Require fresh host reset evidence after a provider ownership change.

        The host may retain messages despite a failed callback or process interruption,
        so durable observed authors alone cannot certify complete resumed history.
        """
        with self.lock, self.db:
            self.db.execute(
                "UPDATE session_provenance SET history_known=0 WHERE collection=?",
                (self.collection,),
            )

    def record_session_author(self, session_id, user_id, explicit=False, *, history_known=None):
        """Persist an observed principal without certifying unobserved history."""
        with self.lock, self.db:
            value = self.session_provenance(session_id).get(
                session_id, {"authors": set(), "explicit_author": False, "history_known": False}
            )
            value["authors"].add(user_id)
            value["explicit_author"] |= explicit
            if history_known is not None:
                value["history_known"] &= history_known
            return self._save_session_provenance(session_id, value)

    def reset_session_provenance(self, session_id, user_id, clear_quarantine=False):
        """Trust a fresh transcript only after the host explicitly discards history."""
        with self.lock, self.db:
            if clear_quarantine:
                self.db.execute(
                    "DELETE FROM unattributed_sessions WHERE session_id=? AND collection=?",
                    (session_id, self.collection),
                )
            return self._save_session_provenance(
                session_id,
                {"authors": {user_id}, "explicit_author": False, "history_known": True},
            )

    def inherit_session_provenance(self, session_id, parent, *, history_known=None):
        """Durably union continuation history without upgrading unknown provenance."""
        with self.lock, self.db:
            source = self.session_provenance(parent).get(
                parent, {"authors": set(), "explicit_author": False, "history_known": False}
            )
            target = self.session_provenance(session_id).get(session_id)
            value = {
                "authors": source["authors"] | (target["authors"] if target else set()),
                "explicit_author": source["explicit_author"]
                or bool(target and target["explicit_author"]),
                "history_known": source["history_known"]
                and (target["history_known"] if target else True)
                and (history_known if history_known is not None else True),
            }
            if self.db.execute(
                "SELECT 1 FROM unattributed_sessions WHERE session_id=? AND collection=?",
                (parent, self.collection),
            ).fetchone():
                self.db.execute(
                    "INSERT OR IGNORE INTO unattributed_sessions VALUES(?,?)",
                    (session_id, self.collection),
                )
            return self._save_session_provenance(session_id, value)

    def backlog_counts(self, authority):
        """Return queued work for one authority and for all attributed events.

        Empty-authority rows are retained Native rejections: they stay
        recoverable but consume neither the shared total nor another
        authority's per-authority budget. The ledger lock is reentrant, so
        callers already inside a transaction may call this safely; the count is
        still only a pre-check because a later ``enqueue_event`` revalidates it.
        """
        with self.lock:
            return self.db.execute(
                "SELECT COALESCE(SUM(CASE WHEN owner_authority=? THEN 1 ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN owner_authority!='' THEN 1 ELSE 0 END), 0) "
                "FROM events WHERE collection=? AND status IN ('PENDING','FAILED','QUARANTINED')",
                (authority, self.collection),
            ).fetchone()

    def enqueue_event(self, value):
        """Persist an immutable event once and return its deterministic key."""
        key = digest([self.collection, value])
        authority = value.get("owner_authority", "")
        if not isinstance(authority, str):
            # Lifecycle plans keep the authority mapping for replay; the
            # per-authority backlog column stores its stable digest.
            authority = digest(authority)
        with self.lock, self.db:
            if authority:
                exists = self.db.execute("SELECT 1 FROM events WHERE event_id=?", (key,)).fetchone()
                if exists is None:
                    row = self.backlog_counts(authority)
                    if (row[0] or 0) >= OWNER_BACKLOG_PER_AUTHORITY or row[
                        1
                    ] >= OWNER_BACKLOG_TOTAL:
                        # Covers direct Native Owner admission as well as the
                        # remote lifecycle _new_plan pre-check.
                        raise OwnerBacklogFull("Owner lifecycle backlog is full")
            self.db.execute(
                "INSERT OR IGNORE INTO events(event_id,session_id,payload_json,created_at,collection,owner_authority) VALUES(?,?,?,?,?,?)",
                (
                    key,
                    value["session_id"],
                    json.dumps(value, ensure_ascii=False),
                    now(),
                    self.collection,
                    authority,
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
            raise ValueError(UNKNOWN_TABLE_MESSAGE)
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
            raise ValueError(UNKNOWN_TABLE_MESSAGE)
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
            raise ValueError(UNKNOWN_TABLE_MESSAGE)
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

    def count_pending_operations(self, keys):
        """Count requested pending keys in small batches without loading payloads.

        Repeated keys retain their multiplicity, matching commit's progress contract.
        Missing, terminal and other-destination operations contribute no count.
        """
        total = 0
        iterator = iter(keys)
        while batch := list(islice(iterator, 128)):
            multiplicities = Counter(batch)
            parameters = list(multiplicities)
            parameters.extend([None] * (128 - len(parameters)))
            with self.lock:
                # Otherwise SQLite can scan the whole pending collection for
                # every key batch. This is the existing TEXT primary-key index.
                cursor = self.db.execute(
                    "SELECT idempotency_key FROM operations INDEXED BY sqlite_autoindex_operations_1 "
                    "WHERE status='PENDING' "
                    "AND collection=? AND idempotency_key IN ("
                    "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                    "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                    "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                    "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?"
                    ")",
                    (self.collection, *parameters),
                )
                try:
                    total += sum(multiplicities[row[0]] for row in cursor)
                finally:
                    cursor.close()
        return total

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

    def quarantine(self, table, key, exc):
        """Retain one unprepared event for operator retry and keep its raw input."""
        if table != "events":
            raise ValueError("Only lifecycle events can be quarantined")
        with self.lock, self.db:
            current = self.db.execute(
                "SELECT payload_json FROM events WHERE event_id=? AND collection=?",
                (key, self.collection),
            ).fetchone()
            if current is None:
                return 0
            plan = json.loads(current["payload_json"])
            if any(field in plan for field in ("candidates", "operation_keys")):
                raise ValueError("A prepared event cannot be quarantined")
            updated = self.db.execute(
                "UPDATE events SET attempts=attempts+1,last_error=?,status='QUARANTINED' "
                "WHERE event_id=? AND collection=? AND status='PENDING'",
                (record_json(exc), key, self.collection),
            ).rowcount
            if updated == 1:
                self.db.execute(
                    "INSERT INTO counters VALUES(?,1,?) "
                    "ON CONFLICT(name,collection) DO UPDATE SET value=value+1",
                    ("events_quarantined", self.collection),
                )
        return updated

    def discard_unadmitted_event(self, key):
        """Atomically discard raw Native input; prepared work cannot cancel."""
        with self.lock:
            row = self.row("events", key)
            if (
                row is None
                or row["collection"] != self.collection
                or row["status"] not in {"PENDING", "FAILED", "CANCELLED"}
            ):
                raise ValueError("Only an unprepared Native admission failure can be discarded")
            if row["status"] == "CANCELLED":
                return {"event_id": key, "status": "CANCELLED", "discarded": False}
            value = json.loads(row["payload_json"])
            if (
                not isinstance(value, dict)
                or value.get("source") != "native_owner_unadmitted"
                or any(
                    field in value for field in ("candidates", "operation_keys", "owner_authority")
                )
            ):
                raise ValueError("Only an unprepared Native admission failure can be discarded")
            with self.db:
                updated = self.db.execute(
                    "UPDATE events SET status='CANCELLED',payload_json='{}',last_error=NULL,committed_at=? "
                    "WHERE event_id=? AND collection=? AND status=? AND payload_json=?",
                    (now(), key, self.collection, row["status"], row["payload_json"]),
                ).rowcount
                if updated != 1:
                    raise ValueError("Native input changed; inspect it again before discarding")
                self.db.execute(
                    "INSERT INTO counters VALUES(?,1,?) ON CONFLICT(name,collection) DO UPDATE SET value=value+1",
                    ("native_unadmitted_discarded", self.collection),
                )
            return {"event_id": key, "status": "CANCELLED", "discarded": True}

    def retry_failed(self):
        """Requeue failed work, including retained quarantined events."""
        with self.lock, self.db:
            for table, condition in (
                ("events", "status IN ('FAILED','QUARANTINED')"),
                ("operations", "status='FAILED'"),
            ):
                self.db.execute(
                    f"UPDATE {table} SET status='PENDING' WHERE {condition} AND collection=?",
                    (self.collection,),
                )

    def retry_failed_event(self, key):
        """Requeue one failed event and only its explicitly prepared operations.

        Remote Owner retry is receipt-scoped and must not use the broad operator
        recovery operation above. Validate the persisted plan before changing any
        status so malformed or cross-collection references fail closed.
        """
        with self.lock, self.db:
            event = self.db.execute(
                "SELECT payload_json FROM events "
                "WHERE event_id=? AND collection=? AND status='FAILED'",
                (key, self.collection),
            ).fetchone()
            if event is None:
                return False
            try:
                value = json.loads(event["payload_json"])
            except (TypeError, ValueError) as exc:
                raise ValueError("Failed event plan is unavailable") from exc
            if not isinstance(value, dict):
                raise ValueError("Failed event plan is unavailable")
            operation_keys = value.get("operation_keys", [])
            if (
                not isinstance(operation_keys, list)
                or any(not isinstance(item, str) or not item for item in operation_keys)
                or len(set(operation_keys)) != len(operation_keys)
            ):
                raise ValueError("Failed event operation plan is invalid")

            operation_statuses = []
            for operation_key in operation_keys:
                operation = self.db.execute(
                    "SELECT status FROM operations WHERE idempotency_key=? AND collection=?",
                    (operation_key, self.collection),
                ).fetchone()
                if operation is None:
                    raise ValueError("Failed event references an unavailable operation")
                status = operation["status"]
                if status not in {"PENDING", "FAILED", "COMMITTED", "SUPERSEDED"}:
                    raise ValueError("Failed event references an invalid operation state")
                operation_statuses.append((operation_key, status))

            for operation_key, status in operation_statuses:
                if status == "FAILED":
                    updated = self.db.execute(
                        "UPDATE operations SET status='PENDING' "
                        "WHERE idempotency_key=? AND collection=? AND status='FAILED'",
                        (operation_key, self.collection),
                    ).rowcount
                    if updated != 1:
                        raise ValueError("Failed event operation changed during retry")
            updated = self.db.execute(
                "UPDATE events SET status='PENDING' "
                "WHERE event_id=? AND collection=? AND status='FAILED'",
                (key, self.collection),
            ).rowcount
            if updated != 1:
                raise ValueError("Failed event changed during retry")
        return True

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

    def snapshot_counts(self, timeout=0.5):
        """Read status counts on a separate connection without taking the writer lock."""
        unavailable = {"available": False, "events": {}, "operations": {}}
        try:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=timeout)
        except (OSError, sqlite3.Error, ValueError):
            return unavailable
        try:
            counts = {
                table: dict(
                    connection.execute(
                        f"SELECT status,COUNT(*) FROM {table} WHERE collection=? GROUP BY status",
                        (self.collection,),
                    )
                )
                for table in ("events", "operations")
            }
        except sqlite3.Error:
            return unavailable
        finally:
            connection.close()
        return {"available": True, **counts}

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
            found = _manifest_operation_keys(row["payload_json"])
            if found is None:
                return None
            keys |= found
        return keys

    def _scrub_terminal_payloads(self, event_keys=None, operation_keys=None):
        """Scrub terminal bodies except operations still needed by an open migration."""
        event_count = self._scrub_event_payloads(event_keys)
        resumable_keys = self._resumable_operation_keys
        if resumable_keys is None:
            return {"events": event_count, "operations": 0}
        return {
            "events": event_count,
            "operations": self._scrub_operation_payloads(resumable_keys, operation_keys),
        }

    def _scrub_event_payloads(self, event_keys):
        """Erase terminal event bodies, one acknowledgement at a time when selected.

        Callers hold ``self.lock``; this helper never acquires it on its own.
        """
        if event_keys is None:
            return self.db.execute(
                """UPDATE events SET payload_json='{}'
                WHERE collection=? AND status IN ('COMMITTED','SUPERSEDED','CANCELLED')
                AND payload_json!='{}'""",
                (self.collection,),
            ).rowcount
        return sum(
            self.db.execute(
                """UPDATE events SET payload_json='{}'
                    WHERE event_id=? AND collection=?
                    AND status IN ('COMMITTED','SUPERSEDED','CANCELLED')
                    AND payload_json!='{}'""",
                (key, self.collection),
            ).rowcount
            for key in event_keys
        )

    def _scrub_operation_payloads(self, resumable_keys, operation_keys):
        """Erase terminal operation bodies an open migration no longer needs.

        Callers hold ``self.lock``; this helper never acquires it on its own.
        """
        if operation_keys is None:
            rows = self._terminal_operation_rows()
        else:
            rows = self._selected_operation_rows(operation_keys)
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
        return operation_count

    def _selected_operation_rows(self, operation_keys):
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

    def _terminal_operation_rows(self, point=None, before=None):
        """Read scrub candidates in stable pages, closing queries before mutations."""
        upper = self.high_watermark("operations") if before is None else before - 1
        after = 0
        while after < upper:
            with self.lock:
                if point is None:
                    cursor = self.db.execute(
                        """SELECT rowid AS scan_rowid,idempotency_key,payload_json
                        FROM operations INDEXED BY operations_unscrubbed_by_collection
                        WHERE collection=? AND status IN ('COMMITTED','SUPERSEDED')
                        AND payload_json!='{}' AND rowid>? AND rowid<=?
                        ORDER BY rowid LIMIT 128""",
                        (self.collection, after, upper),
                    )
                else:
                    cursor = self.db.execute(
                        """SELECT rowid AS scan_rowid,idempotency_key,payload_json
                        FROM operations INDEXED BY operations_unscrubbed_by_point
                        WHERE collection=? AND status IN ('COMMITTED','SUPERSEDED')
                        AND payload_json!='{}' AND rowid>? AND rowid<=? AND point_id=?
                        ORDER BY rowid LIMIT 128""",
                        (self.collection, after, upper, point),
                    )
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
