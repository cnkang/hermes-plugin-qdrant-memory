"""Source-preserving migration, resume and exact-record verification contracts."""

import json

import pytest

from qdrant_memory.migration import (
    create_manifest,
    map_record,
    migrate,
    plan,
    verify_collection,
    verify_manifest,
)
from qdrant_memory.models import Scope
from qdrant_memory.tools import dispatch

from .helpers import runtime


def test_mapping_preserves_provenance_and_unknown_fields():
    """Verify mapping preserves provenance and unknown fields."""
    identifier, value = map_record(
        {
            "id": "local",
            "_cloud_memory_id": "cloud",
            "memory": "cats",
            "user_id": "alice",
            "agent_id": None,
            "run_id": "session",
            "metadata": {"tag": 1},
            "future_field": {"nested": 2},
            "score": 0.9,
        },
        Scope("default", "hermes"),
    )
    assert value["cloud_origin"]["id"] == "cloud"
    assert value["agent_id"] is None
    assert value["session_id"] == "session"
    assert value["metadata"]["legacy_mem0_extra"] == {"future_field": {"nested": 2}}
    assert value["metadata"]["legacy_mem0"] == {"tag": 1}


def test_1000_source_ids_idempotent_and_incremental_update(tmp_path):
    """Verify 1000 source ids idempotent and incremental update."""
    rt = runtime(tmp_path)
    records = [
        {"id": str(i), "memory": f"historical fact {i}", "updated_at": "2026-01-01T00:00:00Z"}
        for i in range(1000)
    ]
    manifest = migrate(rt, records, "mem0-json", "fixture", "checksum", verify=True)
    assert manifest["added"] == manifest["processed"] == 1000
    assert rt.store.count() == 1000
    replay = migrate(rt, records, "mem0-json", "fixture", "checksum", resume=True, verify=True)
    assert replay["migration_id"] == manifest["migration_id"]
    supplemental = [{"id": "5", "memory": "updated fact", "updated_at": "2026-02-01T00:00:00Z"}]
    updated = migrate(rt, supplemental, "mem0-json", "fixture", "new-checksum", verify=True)
    assert updated["updated"] == 1
    assert rt.store.count() == 1000
    assert verify_collection(rt.store)["ok"]
    rt.store.close()
    rt.ledger.close()


def test_failed_migration_resume_and_no_semantic_merge(tmp_path, monkeypatch):
    """Verify failed migration resume and no semantic merge."""
    rt = runtime(tmp_path)
    records = [{"id": str(i), "memory": "identical text"} for i in range(5)]
    original = rt.store.upsert

    def fail(records):
        """Inject a deterministic failure at the exercised boundary."""
        raise ValueError("sentinel-private-response")

    monkeypatch.setattr(rt.store, "upsert", fail)
    with pytest.raises(ValueError):
        migrate(rt, records, "mem0-json", "fixture", "checksum")
    manifest = rt.ledger.manifests()[-1]
    assert manifest["failed"] == 5
    assert "sentinel-private-response" not in json.dumps(rt.ledger.rows("operations", "FAILED"))
    monkeypatch.setattr(rt.store, "upsert", original)
    resumed = migrate(
        rt, records, "mem0-json", "fixture", "checksum", resume=True, retry_failed=True, verify=True
    )
    assert resumed["processed"] == 5
    assert rt.store.count() == 5
    rt.store.close()
    rt.ledger.close()


@pytest.mark.parametrize("updated_at", ["2000-01-01T00:00:00Z", None])
@pytest.mark.parametrize("source_id", ["one", "another"])
def test_fresh_migration_after_delete_preserves_source_time(tmp_path, updated_at, source_id):
    """New imports may restore deleted content without rewriting source timestamps."""
    rt = runtime(tmp_path)
    original = {"id": "one", "memory": "cats preferred", "updated_at": updated_at}
    try:
        first = migrate(rt, [original], "mem0-json", "fixture", "first", verify=True)
        identifier = next(iter(first["records"]))
        scope = Scope(**first["records"][identifier]["scope"])
        dispatch(rt, "qdrant_memory_delete", {"id": identifier}, scope, "session")

        records = [{**original, "id": source_id}]
        imported = migrate(rt, records, "mem0-json", "fixture", "second", verify=True)
        assert imported["processed"] == imported["added"] == 1
        assert imported["failed"] == 0
        assert imported["completed_at"] is not None
        expected = plan(records, rt.cfg)
        for point, value in expected.items():
            assert rt.store.get(point, scope).payload == value
        resumed = migrate(
            rt,
            records,
            "mem0-json",
            "fixture",
            "second",
            resume=True,
            retry_failed=True,
            verify=True,
        )
        assert resumed["migration_id"] == imported["migration_id"]
        assert resumed["processed"] == 1
        assert rt.store.count() == 1
    finally:
        rt.store.close()
        rt.ledger.close()


def test_pre_delete_migration_is_fenced_even_with_future_source_time(tmp_path):
    """A source timestamp cannot make an old prepared import bypass a later delete."""
    rt = runtime(tmp_path)
    records = [{"id": "one", "memory": "cats preferred", "updated_at": "2999-01-01T00:00:00Z"}]
    try:
        planned = plan(records, rt.cfg)
        manifest = create_manifest(rt, planned, "mem0-json", "fixture", "checksum")
        identifier = next(iter(planned))
        scope = Scope(**manifest["records"][identifier]["scope"])
        key = manifest["records"][identifier]["operation_key"]
        with rt.ledger.db:
            rt.ledger.db.execute(
                "UPDATE operations SET created_at=? WHERE idempotency_key=?",
                ("2000-01-01T00:00:00Z", key),
            )
        added = rt.add("cats preferred", scope, session_id="session")
        dispatch(rt, "qdrant_memory_delete", {"id": added["id"]}, scope, "session")
        rt.commit([key])
        assert rt.ledger.row("operations", key)["status"] == "SUPERSEDED"
        assert rt.store.get(identifier, scope) is None
        assert rt.store.count() == 0
    finally:
        rt.store.close()
        rt.ledger.close()


def test_conflicting_source_duplicate_refused(tmp_path):
    """Verify conflicting source duplicate refused."""
    rt = runtime(tmp_path)
    with pytest.raises(ValueError, match="duplicate"):
        plan([{"id": "one", "memory": "old"}, {"id": "one", "memory": "new"}], rt.cfg)
    assert rt.store.count() == 0
    rt.store.close()
    rt.ledger.close()


def test_verifier_detects_missing_point_even_when_count_matches(tmp_path):
    """Verify verifier detects missing point even when count matches."""
    rt = runtime(tmp_path)
    manifest = migrate(rt, [{"id": "one", "memory": "cats"}], "mem0-json", "fixture")
    identifier = next(iter(manifest["records"]))
    rt.store.delete([identifier])
    replacement = {"id": "other", "memory": "cats"}
    migrate(rt, [replacement], "mem0-json", "other")
    result = verify_manifest(rt.store, manifest)
    assert result["target_exact_count"] == 1
    assert not result["ok"]
    rt.store.close()
    rt.ledger.close()
