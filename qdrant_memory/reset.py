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


INTENT_SCHEMA_VERSION = 2
SINGLE_AGENT_MODE = "single_agent"
ALL_AGENTS_MODE = "all_agents"


class ScopeDeleteLegacyIntentError(ValueError):
    """Refuse automatic recovery of an ambiguous pre-versioned deletion intent.

    Version-1 intents stored ``{"user_id": ..., "agent_id": "*"}`` for both a
    single-agent deletion of the literal agent ``'*'`` and an all-agents
    deletion. The two cannot be told apart from the record, so automatic
    recovery must never guess; the operator resolves the intent explicitly.
    """

    def __init__(self, user_id=None):
        """Initialize the actionable disambiguation error."""
        suffix = f" for user {user_id!r}" if user_id else ""
        super().__init__(
            "A pre-versioned scoped-deletion intent is pending"
            f"{suffix}; its recorded form cannot distinguish a single-agent "
            "deletion of agent '*' from an all-agents deletion, so automatic "
            "recovery is refused and no data was modified. Resolve it explicitly "
            "with `hermes qdrant-memory delete-all --user <user> "
            "--resolve-legacy single_agent --confirm` or `--resolve-legacy "
            "all_agents`."
        )


def scope_delete_intent(user_id, agent_id=None, all_agents=False):
    """Return the explicit durable form of a scoped-deletion intent.

    ``agent_id`` may be any string, including a literal ``'*'``, or ``None``
    for the no-agent scope; only ``all_agents`` selects every agent scope.
    """
    if all_agents:
        return {
            "schema_version": INTENT_SCHEMA_VERSION,
            "mode": ALL_AGENTS_MODE,
            "user_id": user_id,
        }
    return {
        "schema_version": INTENT_SCHEMA_VERSION,
        "mode": SINGLE_AGENT_MODE,
        "user_id": user_id,
        "agent_id": agent_id,
    }


def parse_scope_delete_intent(recorded):
    """Return ``(all_agents, user_id, agent_id)`` for an explicit intent.

    Pre-versioned intents are ambiguous and raise
    :class:`ScopeDeleteLegacyIntentError` instead of being guessed.
    """
    if not isinstance(recorded, dict) or recorded.get("schema_version") != INTENT_SCHEMA_VERSION:
        user = recorded.get("user_id") if isinstance(recorded, dict) else None
        raise ScopeDeleteLegacyIntentError(user if isinstance(user, str) else "")
    mode = recorded.get("mode")
    if mode == ALL_AGENTS_MODE:
        return True, recorded.get("user_id"), None
    if mode == SINGLE_AGENT_MODE:
        return False, recorded.get("user_id"), recorded.get("agent_id")
    raise ScopeDeleteRefusedError()


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
            {"scope": row[0], "scope_json": row[1]}
            for row in db.execute(
                "SELECT scope, scope_json FROM qdrant_scope_deletes WHERE collection=?",
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


def resolve_legacy_scope_delete(ledger, old_scope_key, scope):
    """Rewrite a pre-versioned intent into the explicit schema in place.

    The original timestamp anchors every fence identity of the operation, so
    the rewrite preserves it and repeated resumes keep reusing identical
    tombstones instead of accumulating duplicates.
    """
    scope_value = scope.as_dict() if hasattr(scope, "as_dict") else dict(scope)
    new_key = scope_delete_key(scope_value)
    with ledger.lock, ledger.db:
        row = ledger.db.execute(
            "SELECT created_at FROM qdrant_scope_deletes WHERE collection=? AND scope=?",
            (ledger.collection, old_scope_key),
        ).fetchone()
        created = row[0] if row else now()
        ledger.db.execute(
            "DELETE FROM qdrant_scope_deletes WHERE collection=? AND scope=?",
            (ledger.collection, old_scope_key),
        )
        ledger.db.execute(
            "INSERT OR REPLACE INTO qdrant_scope_deletes(collection, scope, scope_json, created_at) "
            "VALUES(?, ?, ?, ?)",
            (ledger.collection, new_key, json.dumps(scope_value), created),
        )
    return created
