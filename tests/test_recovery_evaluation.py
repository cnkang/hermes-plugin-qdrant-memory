"""Persistent embedded crash recovery and synthetic retrieval baseline contracts."""

import subprocess
import sys
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client import models as m

from qdrant_memory.migration import legacy_endpoint, migrate, read_qdrant_records
from qdrant_memory.models import Scope, payload, point_id

from .helpers import runtime


def test_legacy_remote_source_defaults_to_tls():
    """Verify legacy remote source defaults to tls."""
    assert (
        legacy_endpoint({"host": "memory.example", "port": 6333}) == "https://memory.example:6333"
    )
    assert legacy_endpoint({}) == "http://127.0.0.1:6333"
    assert legacy_endpoint({"host": "::1"}) == "http://[::1]:6333"


def test_process_death_after_remote_upsert_before_ack(tmp_path):
    """Verify process death after remote upsert before ack."""
    root = Path(__file__).resolve().parents[1]
    child = """
import os, sys
sys.path.insert(0, sys.argv[1])
from tests.helpers import runtime
from qdrant_client import QdrantClient
from qdrant_memory.models import Scope, payload, point_id
rt = runtime(sys.argv[2], client=QdrantClient(path=sys.argv[3]))
scope = Scope('alice', 'hermes')
key = rt.operation(point_id(scope, 'manual_tool', 'one'), 'UPSERT', payload('cats', scope, 'manual_tool'))
original = rt.store.upsert
def crash(records):
    '''Terminate after confirmed embedded upsert, before ledger acknowledgment.'''
    original(records)
    os._exit(23)
rt.store.upsert = crash
rt.commit([key])
"""
    store_path = tmp_path / "qdrant"
    completed = subprocess.run(
        [sys.executable, "-c", child, str(root), str(tmp_path), str(store_path)], timeout=30
    )
    assert completed.returncode == 23
    rt = runtime(tmp_path, client=QdrantClient(path=str(store_path)))
    assert rt.ledger.stats()["operations"]["PENDING"] == 1
    rt.recover()
    assert rt.store.count() == 1
    assert rt.ledger.stats()["operations"]["COMMITTED"] == 1
    rt.store.close()
    rt.ledger.close()


def test_qdrant_source_is_read_only_and_ids_preserved(tmp_path):
    """Verify qdrant source is read only and ids preserved."""
    source = QdrantClient(":memory:")
    source.create_collection(
        "hermes_mem0", vectors_config=m.VectorParams(size=2, distance=m.Distance.COSINE)
    )
    source.upsert(
        "hermes_mem0",
        points=[
            m.PointStruct(
                id=1,
                vector=[1, 0],
                payload={
                    "memory": "cats",
                    "_cloud_memory_id": "original-cloud",
                    "user_id": "alice",
                },
            )
        ],
    )
    before = source.retrieve("hermes_mem0", ids=[1], with_vectors=True)[0]
    records = list(read_qdrant_records(source, "hermes_mem0"))
    rt = runtime(tmp_path)
    manifest = migrate(rt, records, "mem0-qdrant", "hermes_mem0", verify=True)
    after = source.retrieve("hermes_mem0", ids=[1], with_vectors=True)[0]
    assert after == before
    assert manifest["processed"] == 1
    row = next(rt.store.scroll())
    assert row.payload["cloud_origin"]["id"] == "original-cloud"
    source.close()
    rt.store.close()
    rt.ledger.close()


def test_fixed_dense_retrieval_baseline(tmp_path):
    """Verify fixed dense retrieval baseline."""
    rt = runtime(tmp_path)
    scope = Scope("alice", "hermes")
    fixtures = [
        ("cat", "cats preferred"),
        ("dog", "dogs preferred"),
        ("other", "local infrastructure"),
    ]
    for name, text in fixtures:
        rt.store.upsert(
            [(point_id(scope, "manual_tool", name), payload(text, scope, "manual_tool"))]
        )
    correct = sum(
        rt.store.search(query, scope, top_k=1)[0].payload["text"] == text
        for query, text in fixtures
    )
    assert correct / len(fixtures) == 1.0
    rt.store.close()
    rt.ledger.close()
