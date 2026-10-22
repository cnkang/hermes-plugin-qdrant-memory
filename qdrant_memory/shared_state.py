"""Fail-closed interlock between opt-in shared plans and legacy writer commands."""

import sqlite3
from contextlib import closing
from pathlib import Path


class SharedRecoveryRequiredError(RuntimeError):
    """Require the shared owner to reconcile prepared plans before other mutations."""


def require_no_pending_shared(home, namespace, *, allow_pending_receipts=False):
    """Inspect without creating storage; legacy profiles without these tables are unchanged.

    A receipt whose event is already terminal is a crash leftover, not replay work.
    An authorized destination reset may pass ``allow_pending_receipts`` because the
    reset itself clears those receipts.
    """
    path = Path(home).resolve() / "qdrant-memory" / "state.db"
    if not path.is_file():
        return
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        checks = [
            (
                "shared_requests_v1",
                "SELECT 1 FROM shared_requests_v1 WHERE collection=? AND done=0 LIMIT 1",
            )
        ]
        if not allow_pending_receipts:
            checks.append(
                (
                    "owner_events_v1",
                    """SELECT 1 FROM owner_events_v1
                    WHERE collection=? AND event_json!='{}' AND NOT EXISTS (
                        SELECT 1 FROM events e
                        WHERE e.collection=owner_events_v1.collection
                        AND e.event_id=owner_events_v1.event_key
                        AND e.status IN ('COMMITTED','CANCELLED','SUPERSEDED')
                    ) LIMIT 1""",
                )
            )
        for table, query in checks:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists and db.execute(query, (namespace,)).fetchone():
                raise SharedRecoveryRequiredError()
