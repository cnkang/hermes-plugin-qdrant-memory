"""Bound retained payloads while preserving replay and migration recovery."""

import json

from qdrant_memory.ledger import Ledger
from qdrant_memory.models import Scope, content_hash, now


def test_startup_scrubs_only_terminal_event_and_operation_bodies(tmp_path):
    """Scrub acknowledged event and operation bodies on ledger startup."""
    ledger = Ledger(tmp_path)
    event_ids = {}
    operation_keys = {}
    event_values = {}
    operation_values = {}

    for status in ("PENDING", "FAILED", "COMMITTED"):
        event = {"session_id": status, "user": f"private event {status}"}
        key = ledger.enqueue_event(event)
        event_ids[status] = key
        event_values[status] = event
        ledger.db.execute("UPDATE events SET status=? WHERE event_id=?", (status, key))

    for status in ("PENDING", "FAILED", "COMMITTED", "SUPERSEDED"):
        value = {"text": f"private operation {status}"}
        key = ledger.enqueue_operation(f"point-{status}", "UPSERT", value)
        operation_keys[status] = key
        operation_values[status] = value
        ledger.db.execute("UPDATE operations SET status=? WHERE idempotency_key=?", (status, key))
    ledger.db.commit()
    ledger.close()

    ledger = Ledger(tmp_path)
    try:
        for status, key in event_ids.items():
            row = ledger.row("events", key)
            assert row["status"] == status
            body = json.loads(row["payload_json"])
            if status == "COMMITTED":
                assert body == {}
                assert ledger.enqueue_event(event_values[status]) == key
                assert ledger.row("events", key)["status"] == "COMMITTED"
            else:
                assert body == event_values[status]

        for status, key in operation_keys.items():
            row = ledger.row("operations", key)
            assert row["status"] == status
            body = json.loads(row["payload_json"])
            if status in {"COMMITTED", "SUPERSEDED"}:
                assert body == {}
            else:
                assert body == operation_values[status]
    finally:
        ledger.close()


def test_resumable_migration_keeps_operation_body_until_manifest_completes(tmp_path):
    """Retain payloads still referenced by incomplete migration manifests."""
    ledger = Ledger(tmp_path)
    value = {"text": "migration payload retained for resume"}
    key = ledger.enqueue_operation("source-point", "UPSERT", value, "mem0:source", "v1")
    manifest = {
        "migration_id": "migration-1",
        "completed_at": None,
        "records": {"source-point": {"operation_key": key}},
    }
    try:
        ledger.save_manifest(manifest)
        ledger.finish("operations", key)
        assert json.loads(ledger.row("operations", key)["payload_json"]) == value

        manifest["completed_at"] = now()
        ledger.save_manifest(manifest)
        row = ledger.row("operations", key)
        assert row["status"] == "COMMITTED"
        assert json.loads(row["payload_json"]) == {}
    finally:
        ledger.close()


def test_delete_fence_is_bound_to_scope_and_event_admission_time(tmp_path):
    """Apply the delete fence only to old events in the matching scope."""
    ledger = Ledger(tmp_path)
    alice = Scope("alice", "hermes")
    bob = Scope("bob", "hermes")
    try:
        source_id = ledger.delete_fence_source(alice)
        deleted_hash = content_hash("cats preferred")
        key = ledger.enqueue_operation(
            "shared-point", "DELETE", {"content_hash": deleted_hash}, source_id=source_id
        )
        fence_time = ledger.row("operations", key)["created_at"]

        assert ledger.is_delete_fenced("shared-point", alice, "2000-01-01T00:00:00Z")
        assert ledger.is_delete_fenced(
            "different-source-point", alice, "2000-01-01T00:00:00Z", deleted_hash
        )
        assert not ledger.is_delete_fenced(
            "different-source-point", alice, "2000-01-01T00:00:00Z", content_hash("dogs")
        )
        assert not ledger.is_delete_fenced("shared-point", bob, "2000-01-01T00:00:00Z")
        assert not ledger.is_delete_fenced("shared-point", alice, "2999-01-01T00:00:00Z")
        assert fence_time
    finally:
        ledger.close()


def test_ledger_retained_history_uses_targeted_lookup_indexes(tmp_path):
    """Avoid scanning retained rows for replay, migration, and delete-fence lookups."""
    ledger = Ledger(tmp_path)
    try:
        plans = {
            "events": ledger.db.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM events WHERE status=? AND collection=? ORDER BY rowid",
                ("PENDING", ledger.collection),
            ).fetchall(),
            "operations": ledger.db.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM operations WHERE status=? AND collection=? ORDER BY rowid",
                ("PENDING", ledger.collection),
            ).fetchall(),
            "open_point": ledger.db.execute(
                """EXPLAIN QUERY PLAN SELECT 1 FROM operations
                WHERE point_id=? AND collection=? AND status IN ('PENDING','FAILED') LIMIT 1""",
                ("point", ledger.collection),
            ).fetchall(),
            "delete_fence_point": ledger.db.execute(
                """EXPLAIN QUERY PLAN SELECT source_version,created_at FROM operations
                WHERE action='DELETE' AND source_id=? AND collection=? AND point_id=?""",
                (ledger.delete_fence_source(Scope("alice", "hermes")), ledger.collection, "point"),
            ).fetchall(),
            "delete_fence_hash": ledger.db.execute(
                """EXPLAIN QUERY PLAN SELECT source_version,created_at FROM operations
                WHERE action='DELETE' AND source_id=? AND collection=? AND content_hash=?""",
                (ledger.delete_fence_source(Scope("alice", "hermes")), ledger.collection, "hash"),
            ).fetchall(),
        }
        details = {name: [row["detail"] for row in rows] for name, rows in plans.items()}

        assert any("events_by_collection_status" in detail for detail in details["events"])
        assert any("operations_by_collection_status" in detail for detail in details["operations"])
        assert any("operations_by_point_status" in detail for detail in details["open_point"])
        assert any(
            "operations_delete_by_point" in detail for detail in details["delete_fence_point"]
        )
        assert any("operations_delete_by_hash" in detail for detail in details["delete_fence_hash"])
    finally:
        ledger.close()


def test_opening_existing_ledger_adds_indexes_without_losing_rows(tmp_path):
    """Upgrade an existing ledger schema in place and preserve its replay rows."""
    ledger = Ledger(tmp_path)
    event_key = ledger.enqueue_event({"session_id": "session", "user": "keep this"})
    operation_key = ledger.enqueue_operation("point", "UPSERT", {"text": "keep this"})
    with ledger.db:
        for name in (
            "events_by_collection_status",
            "operations_by_collection_status",
            "operations_by_point_status",
            "operations_delete_by_point",
            "operations_delete_by_hash",
        ):
            ledger.db.execute(f"DROP INDEX {name}")
    ledger.close()

    upgraded = Ledger(tmp_path)
    try:
        names = {row[1] for row in upgraded.db.execute("PRAGMA index_list(operations)")}
        assert {
            "operations_by_collection_status",
            "operations_by_point_status",
            "operations_delete_by_point",
            "operations_delete_by_hash",
        } <= names
        event_names = {row[1] for row in upgraded.db.execute("PRAGMA index_list(events)")}
        assert "events_by_collection_status" in event_names
        assert upgraded.row("events", event_key)["payload_json"]
        assert upgraded.row("operations", operation_key)["payload_json"]
    finally:
        upgraded.close()
