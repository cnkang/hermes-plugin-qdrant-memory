import pytest
from qdrant_client import QdrantClient, models as m
from qdrant_memory.models import Scope, payload, point_id
from qdrant_memory.dedupe import decide
from qdrant_memory.qdrant_store import QdrantStore
from qdrant_memory.tools import dispatch
from qdrant_memory.ledger import Ledger
from qdrant_memory.runtime import Runtime
from .helpers import Embedder, LLM, config, runtime


def test_scope_isolation_and_wrong_scope_tool_delete(tmp_path):
    rt = runtime(tmp_path)
    alice, bob = Scope("alice", "hermes"), Scope("bob", "hermes")
    identifier = point_id(alice, "manual_tool", "one")
    rt.store.upsert([(identifier, payload("cats preferred", alice, "manual_tool"))])
    assert rt.store.search("cats", alice)
    assert not rt.store.search("cats", bob)
    with pytest.raises(ValueError, match="scope"):
        dispatch(rt, "qdrant_memory_delete", {"id": identifier}, bob, "s")
    assert rt.store.count() == 1
    dispatch(rt, "qdrant_memory_update", {"id": identifier, "text": "dogs preferred"}, alice, "s")
    assert rt.store.get(identifier, alice).payload["text"] == "dogs preferred"
    rt.store.close(); rt.ledger.close()


@pytest.mark.parametrize("relation,action", [("SAME", "SKIP"), ("SUPERSEDES", "UPDATE"), ("CONFLICT", "ADD"), ("UNRELATED", "ADD")])
def test_dedupe_never_assumes_similarity_is_identity(tmp_path, relation, action):
    llm = LLM(relation)
    rt = runtime(tmp_path, llm=llm)
    scope = Scope("alice", "hermes")
    identifier = point_id(scope, "conversation", "cat")
    rt.store.upsert([(identifier, payload("cats preferred", scope, "conversation"))])
    result = decide("cats avoided", scope, rt.store, rt.extractor, rt.cfg["dedupe"])
    assert result[0] == action
    assert llm.calls[-1]["purpose"] == "qdrant-memory.relation"
    rt.store.close(); rt.ledger.close()


def test_dimension_and_fingerprint_mismatch_refuse(tmp_path):
    client = QdrantClient(":memory:")
    cfg = config()
    client.create_collection(cfg["qdrant"]["collection"], vectors_config={"dense": m.VectorParams(size=2, distance=m.Distance.COSINE)})
    store = QdrantStore(client, cfg, Embedder())
    with pytest.raises(ValueError, match="dimension"):
        store.initialize()
    client.close()
    rt = runtime(tmp_path)
    changed = Embedder(); changed.fingerprint = "different-model-same-dims"
    store = QdrantStore(rt.store.client, rt.cfg, changed)
    with pytest.raises(ValueError, match="fingerprint"):
        store.initialize()
    rt.store.close(); rt.ledger.close()


def test_pending_operation_resume_and_collection_partition(tmp_path):
    rt = runtime(tmp_path)
    scope = Scope("alice", "hermes")
    key = rt.operation(point_id(scope, "manual_tool", "one"), "UPSERT", payload("cats", scope, "manual_tool"))
    other = Ledger(tmp_path, "other-collection")
    assert other.rows("operations") == []
    rt.ledger.close()
    rt = Runtime(rt.cfg, rt.store, Ledger(tmp_path), rt.extractor)
    rt.recover()
    assert rt.ledger.row("operations", key)["status"] == "COMMITTED"
    rt.recover()
    assert rt.store.count() == 1
    other.close(); rt.store.close(); rt.ledger.close()


def test_terminal_write_records_one_failure_attempt(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    scope = Scope("alice", "hermes")
    key = rt.operation(point_id(scope, "manual_tool", "one"), "UPSERT", payload("cats", scope, "manual_tool"))
    def fail(records):
        raise ValueError("invalid remote payload")
    monkeypatch.setattr(rt.store, "upsert", fail)
    with pytest.raises(ValueError):
        rt.commit([key])
    row = rt.ledger.row("operations", key)
    assert row["status"] == "FAILED"
    assert row["attempts"] == 1
    rt.store.close()
    rt.ledger.close()
