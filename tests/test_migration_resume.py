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
        records = [{"id": "one", "memory": "current"}]
        migrate(rt, records, "mem0-json", "fixture")
        skipped = migrate(rt, records, "mem0-json", "fixture")
        resumed = migrate(rt, records, "mem0-json", "fixture", resume=True, verify=True)
        assert resumed["migration_id"] == skipped["migration_id"]
        assert resumed["skipped"] == 1
    finally:
        rt.store.close()
        rt.ledger.close()
