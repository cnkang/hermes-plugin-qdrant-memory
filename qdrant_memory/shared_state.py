"""Fail-closed interlock between opt-in shared plans and legacy writer commands."""

import sqlite3
from contextlib import closing
from pathlib import Path


class SharedRecoveryRequiredError(RuntimeError):
    """Require the shared owner to reconcile prepared plans before other mutations."""


def require_no_pending_shared(home, namespace):
    """Inspect without creating storage; legacy profiles without this table are unchanged."""
    path = Path(home).resolve() / "qdrant-memory" / "state.db"
    if not path.is_file():
        return
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shared_requests_v1'"
        ).fetchone()
        if (
            exists
            and db.execute(
                "SELECT 1 FROM shared_requests_v1 WHERE collection=? AND done=0 LIMIT 1",
                (namespace,),
            ).fetchone()
        ):
            raise SharedRecoveryRequiredError()
