"""Private reset must discard admission receipts that can replay raw input."""

import json

import pytest

from qdrant_memory.owner_lifecycle import OwnerLifecycle
from qdrant_memory.shared_service import SharedOwner
from qdrant_memory.shared_state import SharedRecoveryRequiredError, require_no_pending_shared

from ..helpers import grant, runtime


def test_reset_clears_admission_receipts_and_unblocks_destruction(tmp_path):
    """A retained lifecycle receipt is replay work and must not survive a reset."""
    rt = runtime(tmp_path)
    OwnerLifecycle(SharedOwner(rt, [grant("owner", visibility="private", logical="agent")[1]]))
    with rt.ledger.lock, rt.ledger.db:
        rt.ledger.db.execute(
            "INSERT INTO owner_events_v1 VALUES(?,?,?,?,?)",
            (rt.ledger.collection, "request", "input", "event", json.dumps({"user": "SECRET"})),
        )
    with pytest.raises(SharedRecoveryRequiredError):
        require_no_pending_shared(tmp_path, rt.ledger.collection)

    rt.ledger.clear_destination()
    assert rt.ledger.db.execute("SELECT COUNT(*) FROM owner_events_v1").fetchone()[0] == 0
    require_no_pending_shared(tmp_path, rt.ledger.collection)
