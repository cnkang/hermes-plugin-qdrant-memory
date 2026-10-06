"""Recheck skipped migration records against live target and ledger state."""

import pytest

from qdrant_memory.migration import map_record, migrate
from qdrant_memory.models import Scope

from .helpers import runtime


@pytest.mark.parametrize("change", ["delete", "modify", "pending", "failed"])
def test_resume_repairs_stale_skip(tmp_path, change):
    """Replan skipped records if target data changes or unsettled writes appear."""
    rt = runtime(tmp_path)
    try:
        records = [{"id": "one", "memory": "current"}]
        migrate(rt, records, "mem0-json", "fixture")
        skipped = migrate(rt, records, "mem0-json", "fixture")
        assert skipped["skipped"] == 1
        identifier = next(iter(skipped["records"]))
        _, stale = map_record({"id": "one", "memory": "stale"}, Scope(**rt.cfg["scope"]))
        key = None
        if change == "delete":
            rt.store.delete([identifier])
        elif change == "modify":
            rt.store.upsert([(identifier, stale)])
        else:
            key = rt.operation(identifier, "UPSERT", stale)
            if change == "failed":
                rt.ledger.failure("operations", key, ValueError("invalid"), terminal=True)
        resumed = migrate(rt, records, "mem0-json", "fixture", resume=True, verify=True)
        assert resumed["migration_id"] != skipped["migration_id"]
        if key:
            assert rt.ledger.row("operations", key)["status"] == "SUPERSEDED"
        rt.ledger.retry_failed()
        rt.recover()
        assert rt.store.get(identifier, Scope(**rt.cfg["scope"])).payload["text"] == "current"
    finally:
        rt.store.close()
        rt.ledger.close()


def test_resume_retains_settled_skip(tmp_path):
    """Reuse an unchanged skip without generating another migration."""
    rt = runtime(tmp_path)
    try:
        _assert_settled_skip_is_reused(rt)
    finally:
        rt.store.close()
        rt.ledger.close()


def _assert_settled_skip_is_reused(rt):
    """Assert a resumed migration reuses a settled skip instead of replanning it.

    Args:
        rt: Runtime fixture providing the store, ledger and migration config.
    """
    records = [{"id": "one", "memory": "current"}]
    migrate(rt, records, "mem0-json", "fixture")
    skipped = migrate(rt, records, "mem0-json", "fixture")
    resumed = migrate(rt, records, "mem0-json", "fixture", resume=True, verify=True)
    assert resumed["migration_id"] == skipped["migration_id"]
    assert resumed["skipped"] == 1


def test_resume_repairs_committed_drift(tmp_path):
    """Resume after external deletion of a committed point triggers fresh re-commit."""
    rt = runtime(tmp_path)
    try:
        _assert_committed_drift_is_repaired(rt)
    finally:
        rt.store.close()
        rt.ledger.close()


def _assert_committed_drift_is_repaired(rt):
    """Assert a resumed migration re-commits a point deleted outside the ledger.

    Args:
        rt: Runtime fixture providing the store, ledger and migration config.
    """
    records = [{"id": "one", "memory": "current"}]
    first = migrate(rt, records, "mem0-json", "fixture")
    assert first["processed"] == 1
    identifier = next(iter(first["records"]))
    rt.store.delete([identifier])
    resumed = migrate(rt, records, "mem0-json", "fixture", resume=True, verify=True)
    assert resumed["migration_id"] != first["migration_id"]
    assert resumed["processed"] == 1
    point = rt.store.get(identifier, Scope(**rt.cfg["scope"]))
    assert point is not None
    assert point.payload["text"] == "current"
