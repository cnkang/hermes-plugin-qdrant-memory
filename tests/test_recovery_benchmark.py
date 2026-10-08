"""The reproducible benchmark must explain both real recovery reader queries."""

from qdrant_memory.ledger import Ledger
from scripts.benchmark_recovery import query_plans


def test_benchmark_explains_both_parameterized_queries(tmp_path):
    """Static EXPLAIN statements retain collection/status index and keyset bounds."""
    ledger = Ledger(tmp_path)
    try:
        plans = query_plans(ledger)
        assert set(plans) == {"legacy", "bounded"}
        assert all(
            any("operations_by_collection_status" in detail for detail in plan)
            for plan in plans.values()
        )
        assert any("rowid>? AND rowid<?" in detail for detail in plans["bounded"])
        assert not any("TEMP B-TREE" in detail for detail in plans["bounded"])
    finally:
        ledger.close()
