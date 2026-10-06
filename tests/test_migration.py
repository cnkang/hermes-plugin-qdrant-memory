import json
import pytest
from qdrant_memory.migration import map_record, migrate, plan, verify_manifest, verify_collection
from qdrant_memory.models import Scope
from .helpers import runtime


def test_mapping_preserves_provenance_and_unknown_fields():
    identifier, value = map_record({"id": "local", "_cloud_memory_id": "cloud", "memory": "cats",
                                   "user_id": "alice", "agent_id": None, "run_id": "session",
                                   "metadata": {"tag": 1}, "future_field": {"nested": 2}, "score": 0.9}, Scope("default", "hermes"))
    assert value["cloud_origin"]["id"] == "cloud"
    assert value["agent_id"] is None
    assert value["session_id"] == "session"
    assert value["metadata"]["legacy_mem0_extra"] == {"future_field": {"nested": 2}}
    assert value["metadata"]["legacy_mem0"] == {"tag": 1}


def test_1000_source_ids_idempotent_and_incremental_update(tmp_path):
    rt = runtime(tmp_path)
    records = [{"id": str(i), "memory": f"historical fact {i}", "updated_at": "2026-01-01T00:00:00Z"} for i in range(1000)]
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
    rt.store.close(); rt.ledger.close()


def test_failed_migration_resume_and_no_semantic_merge(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    records = [{"id": str(i), "memory": "identical text"} for i in range(5)]
    original = rt.store.upsert
    def fail(records):
        raise ValueError("sentinel-private-response")
    monkeypatch.setattr(rt.store, "upsert", fail)
    with pytest.raises(ValueError):
        migrate(rt, records, "mem0-json", "fixture", "checksum")
    manifest = rt.ledger.manifests()[-1]
    assert manifest["failed"] == 5
    assert "sentinel-private-response" not in json.dumps(rt.ledger.rows("operations", "FAILED"))
    monkeypatch.setattr(rt.store, "upsert", original)
    resumed = migrate(rt, records, "mem0-json", "fixture", "checksum", resume=True, retry_failed=True, verify=True)
    assert resumed["processed"] == 5
    assert rt.store.count() == 5
    rt.store.close(); rt.ledger.close()


def test_conflicting_source_duplicate_refused(tmp_path):
    rt = runtime(tmp_path)
    with pytest.raises(ValueError, match="duplicate"):
        plan([{"id": "one", "memory": "old"}, {"id": "one", "memory": "new"}], rt.cfg)
    assert rt.store.count() == 0
    rt.store.close(); rt.ledger.close()


def test_verifier_detects_missing_point_even_when_count_matches(tmp_path):
    rt = runtime(tmp_path)
    manifest = migrate(rt, [{"id": "one", "memory": "cats"}], "mem0-json", "fixture")
    identifier = next(iter(manifest["records"]))
    rt.store.delete([identifier])
    replacement = {"id": "other", "memory": "cats"}
    migrate(rt, [replacement], "mem0-json", "other")
    result = verify_manifest(rt.store, manifest)
    assert result["target_exact_count"] == 1
    assert not result["ok"]
    rt.store.close(); rt.ledger.close()
