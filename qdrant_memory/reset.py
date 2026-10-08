"""Durable, destination-scoped recovery for destructive collection resets."""

import sqlite3
from pathlib import Path

RESET_INTENTS_TABLE = "qdrant_reset_intents"


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
