"""Private reset must discard admission receipts that can replay raw input."""

import json
from types import SimpleNamespace

import pytest

from qdrant_memory import cli
from qdrant_memory.owner_lifecycle import OwnerLifecycle
from qdrant_memory.shared_service import SharedOwner
from qdrant_memory.shared_state import SharedRecoveryRequiredError, require_no_pending_shared

from ..helpers import grant, runtime


def _insert_receipt(rt, collection, request="request", event="event"):
    with rt.ledger.lock, rt.ledger.db:
        rt.ledger.db.execute(
            "INSERT INTO owner_events_v1 VALUES(?,?,?,?,?)",
            (collection, request, "input", event, json.dumps({"user": "SECRET"})),
        )


def test_reset_clears_only_this_destination_receipts(tmp_path):
    """A reset clears current receipts and leaves another destination untouched."""
    rt = runtime(tmp_path)
    OwnerLifecycle(SharedOwner(rt, [grant("owner", visibility="private", logical="agent")[1]]))
    other = rt.ledger.collection + "-other"
    _insert_receipt(rt, rt.ledger.collection)
    _insert_receipt(rt, other, request="other-request", event="other-event")
    with rt.ledger.lock, rt.ledger.db:
        rt.ledger.db.execute(
            "CREATE TABLE IF NOT EXISTS shared_requests_v1 (collection TEXT, request_key TEXT,"
            " input_hash TEXT, plan TEXT, response TEXT, done INTEGER, principal TEXT,"
            " PRIMARY KEY(collection, request_key))"
        )
        rt.ledger.db.execute(
            "INSERT INTO shared_requests_v1 VALUES(?,?,?,?,?,?,?)",
            (rt.ledger.collection, "k1", "h1", "null", "{}", 1, "p"),
        )
        rt.ledger.db.execute(
            "INSERT INTO shared_requests_v1 VALUES(?,?,?,?,?,?,?)",
            (other, "k2", "h2", "null", "{}", 0, "p"),
        )

    with pytest.raises(SharedRecoveryRequiredError):
        require_no_pending_shared(tmp_path, rt.ledger.collection)
    rt.ledger.clear_destination()
    counts = {
        table: rt.ledger.db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE collection=?", (rt.ledger.collection,)
        ).fetchone()[0]
        for table in ("owner_events_v1", "shared_requests_v1")
    }
    assert counts == {"owner_events_v1": 0, "shared_requests_v1": 0}
    assert (
        rt.ledger.db.execute(
            "SELECT COUNT(*) FROM owner_events_v1 WHERE collection=?", (other,)
        ).fetchone()[0]
        == 1
    )
    require_no_pending_shared(tmp_path, rt.ledger.collection)


def test_terminal_event_receipt_does_not_block_startup(tmp_path):
    """A crash between commit and receipt scrub is not pending replay work."""
    rt = runtime(tmp_path)
    OwnerLifecycle(SharedOwner(rt, [grant("owner", visibility="private", logical="agent")[1]]))
    _insert_receipt(rt, rt.ledger.collection, request="r", event="committed-event")
    with rt.ledger.lock, rt.ledger.db:
        rt.ledger.db.execute(
            "INSERT INTO events(event_id,payload_json,status,collection) VALUES(?,?,?,?)",
            ("committed-event", "{}", "COMMITTED", rt.ledger.collection),
        )
    require_no_pending_shared(tmp_path, rt.ledger.collection)


def test_plain_init_does_not_skip_pending_receipts(tmp_path, monkeypatch):
    """Only an authorized clear may pass the receipt interlock."""
    rt = runtime(tmp_path)
    OwnerLifecycle(SharedOwner(rt, [grant("owner", visibility="private", logical="agent")[1]]))
    _insert_receipt(rt, rt.ledger.collection)
    monkeypatch.setattr(cli, "ledger_namespace", lambda cfg: rt.ledger.collection)

    class Store:
        collection = rt.ledger.collection
        client = SimpleNamespace(collection_exists=lambda name: False)

    class Resources:
        def callback(self, function):
            return None

    args = SimpleNamespace(qdrant_command="init")
    with pytest.raises(SharedRecoveryRequiredError):
        cli._initialize_target(
            args, Store(), tmp_path, rt.cfg, False, Resources(), lambda *a, **k: None
        )
