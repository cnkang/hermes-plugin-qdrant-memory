"""Fail-closed interlock between opt-in shared plans and legacy writer commands."""

import sqlite3
from contextlib import closing
from pathlib import Path


class SharedRecoveryRequiredError(RuntimeError):
    """Require the shared owner to reconcile prepared plans before other mutations."""


def pending_shared(db, namespace):
    """Count unresolved shared work in one read-only ledger connection.

    A receipt whose event is already terminal is a crash leftover, not replay work.
    The same condition serves every caller so the legacy interlock and the upgrade
    planner cannot drift apart.
    """
    counts = {"shared_requests": 0, "owner_receipts": 0}
    checks = {
        "shared_requests_v1": (
            "shared_requests",
            "SELECT COUNT(*) FROM shared_requests_v1 WHERE collection=? AND done=0",
        ),
        "owner_events_v1": (
            "owner_receipts",
            """SELECT COUNT(*) FROM owner_events_v1
            WHERE collection=? AND event_json!='{}' AND NOT EXISTS (
                SELECT 1 FROM events e
                WHERE e.collection=owner_events_v1.collection
                AND e.event_id=owner_events_v1.event_key
                AND e.status IN ('COMMITTED','CANCELLED','SUPERSEDED','QUARANTINED')
            )""",
        ),
    }
    for table, (key, query) in checks.items():
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists:
            counts[key] = db.execute(query, (namespace,)).fetchone()[0]
    return counts


def inspect_pending_shared(home, namespace):
    """Return the counts without creating storage or opening the destination."""
    path = Path(home).resolve() / "qdrant-memory" / "state.db"
    if not path.is_file():
        return {"shared_requests": 0, "owner_receipts": 0}
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        return pending_shared(db, namespace)


def require_no_pending_shared(home, namespace):
    """Inspect without creating storage; legacy profiles without these tables are unchanged."""
    if any(inspect_pending_shared(home, namespace).values()):
        raise SharedRecoveryRequiredError()
