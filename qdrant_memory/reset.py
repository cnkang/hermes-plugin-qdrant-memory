"""Durable, destination-scoped recovery for destructive collection resets."""

import json
import sqlite3
from pathlib import Path

from .models import digest, now

RESET_INTENTS_TABLE = "qdrant_reset_intents"
SCOPE_DELETES_TABLE = "qdrant_scope_deletes"


class ResetRecoveryRequiredError(ValueError):
    """Fail closed while a previously authorized destination reset is pending."""

    def __init__(self):
        """Initialize the actionable recovery error."""
        super().__init__(
            "An interrupted Qdrant collection reset is pending; rerun "
            "`hermes qdrant-memory init` to resume it before using this destination."
        )


def has_pending_reset(ledger):
    """Return whether this ledger destination has a durable reset intent."""
    with ledger.lock:
        exists = ledger.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (RESET_INTENTS_TABLE,),
        ).fetchone()
        if not exists:
            return False
        return (
            ledger.db.execute(
                "SELECT 1 FROM qdrant_reset_intents WHERE collection=?",
                (ledger.collection,),
            ).fetchone()
            is not None
        )


def has_pending_reset_file(home, collection):
    """Inspect an existing ledger for a reset intent without creating or mutating it."""
    path = Path(home) / "qdrant-memory" / "state.db"
    if not path.is_file():
        return False
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (RESET_INTENTS_TABLE,),
        ).fetchone()
        if not exists:
            return False
        return (
            db.execute(
                "SELECT 1 FROM qdrant_reset_intents WHERE collection=?", (collection,)
            ).fetchone()
            is not None
        )


def begin_destination_reset(ledger):
    """Commit reset intent before any remote collection deletion can begin."""
    with ledger.lock, ledger.db:
        ledger.db.execute(
            """CREATE TABLE IF NOT EXISTS qdrant_reset_intents (
                collection TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        ledger.db.execute(
            "INSERT OR IGNORE INTO qdrant_reset_intents(collection) VALUES(?)",
            (ledger.collection,),
        )


def resume_destination_reset(store, ledger):
    """Rebuild and validate the target, then discard only its local replay work.

    The intent remains durable when probing, deletion, recreation, schema checks,
    identity checks or index creation fail. Ledger rows are removed only after the
    store initialization contract succeeds; the intent is deleted after that cleanup.
    """
    if not has_pending_reset(ledger):
        raise ResetRecoveryRequiredError()
    store.initialize(create=True, reset=True)
    ledger.clear_destination()
    with ledger.lock, ledger.db:
        ledger.db.execute(
            "DELETE FROM qdrant_reset_intents WHERE collection=?",
            (ledger.collection,),
        )


class ScopeDeleteRecoveryRequiredError(ValueError):
    """Fail closed while a previously authorized scoped deletion is pending."""

    def __init__(self):
        """Initialize the actionable recovery error."""
        super().__init__(
            "An interrupted scoped deletion is pending; rerun "
            "`hermes qdrant-memory delete-all --confirm` to resume it "
            "before using this destination."
        )


class ScopeDeleteRefusedError(ValueError):
    """Refuse a scoped deletion that cannot be authorized or completed safely."""


class ScopeDeleteIncompleteError(ValueError):
    """Report a scoped deletion that ended with points still stored."""

    def __init__(self):
        """Initialize the actionable incomplete-deletion error."""
        super().__init__(
            "Scoped deletion incomplete: the scope still contains points. "
            "The durable intent remains; rerun `hermes qdrant-memory "
            "delete-all --confirm` to resume it."
        )


def scope_delete_key(scope):
    """Return the durable identity of one scoped deletion."""
    scope_value = scope.as_dict() if hasattr(scope, "as_dict") else dict(scope)
    return digest(scope_value)


def _scope_deletes_table_exists(db):
    """Return whether a database already records scoped-deletion intents."""
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (SCOPE_DELETES_TABLE,),
        ).fetchone()
        is not None
    )


def pending_scope_deletes(ledger):
    """List this destination's recorded scoped-deletion intents with their scopes."""
    with ledger.lock:
        if not _scope_deletes_table_exists(ledger.db):
            return []
        return [
            dict(row)
            for row in ledger.db.execute(
                "SELECT scope, scope_json, created_at FROM qdrant_scope_deletes WHERE collection=?",
                (ledger.collection,),
            )
        ]


def pending_scope_deletes_file(home, collection):
    """Inspect an existing ledger for scoped-deletion intents without mutating it."""
    path = Path(home) / "qdrant-memory" / "state.db"
    if not path.is_file():
        return []
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as db:
        if not _scope_deletes_table_exists(db):
            return []
        return [
            row[0]
            for row in db.execute(
                "SELECT scope FROM qdrant_scope_deletes WHERE collection=?",
                (collection,),
            )
        ]


def begin_scope_delete(ledger, scope):
    """Commit a scoped-deletion intent before any remote deletion can begin.

    Returns the durable intent timestamp so every resume reuses identical
    fence identities instead of accumulating duplicate tombstones.
    """
    scope_value = scope.as_dict() if hasattr(scope, "as_dict") else dict(scope)
    key = scope_delete_key(scope_value)
    with ledger.lock, ledger.db:
        ledger.db.execute(
            """CREATE TABLE IF NOT EXISTS qdrant_scope_deletes (
                collection TEXT NOT NULL,
                scope TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (collection, scope)
            )"""
        )
        ledger.db.execute(
            "INSERT OR IGNORE INTO qdrant_scope_deletes(collection, scope, scope_json, created_at) "
            "VALUES(?, ?, ?, ?)",
            (ledger.collection, key, json.dumps(scope_value), now()),
        )
        row = ledger.db.execute(
            "SELECT created_at FROM qdrant_scope_deletes WHERE collection=? AND scope=?",
            (ledger.collection, key),
        ).fetchone()
    return row[0] if row else ""


def finish_scope_delete(ledger, scope_key):
    """Clear the intent only after the scoped deletion fully completed."""
    with ledger.lock, ledger.db:
        ledger.db.execute(
            "DELETE FROM qdrant_scope_deletes WHERE collection=? AND scope=?",
            (ledger.collection, scope_key),
        )
