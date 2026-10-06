"""Expose durable decision counts and recall metrics while providers are active."""

from qdrant_memory.ledger import Ledger
from qdrant_memory.models import Scope

from .helpers import runtime
from .test_provider import provider


def test_decisions_survive_runtime_restart(tmp_path):
    """ADD and SKIP counters remain available to a fresh ledger reader."""
    rt = runtime(tmp_path)
    rt.add("cats preferred", Scope("u", None))
    rt.add("cats preferred", Scope("u", None))
    rt.store.close()
    rt.ledger.close()
    reader = Ledger(tmp_path, rt.store.collection)
    try:
        assert reader.stats()["dedupe"] == {"ADD": 1, "SKIP": 1}
    finally:
        reader.close()


def test_prefetch_rate_is_visible_before_shutdown(tmp_path):
    """A live reader sees both recall misses and hits without worker exit."""
    p = provider(tmp_path)
    try:
        p.prefetch("cats")
        p.queue_prefetch("cats")
        assert p.wait_idle()
        p.prefetch("cats")
        reader = Ledger(tmp_path, p.ledger.collection)
        try:
            assert reader.metrics()["prefetch_cache_hit_rate"]["samples"] == 2
        finally:
            reader.close()
    finally:
        p.shutdown()
