"""Remote-client configuration and destination isolation contracts."""

from unittest.mock import Mock

import pytest

from qdrant_memory.qdrant_store import build_client

from .helpers import config


@pytest.mark.parametrize(
    "mode,url", [("server", "http://127.0.0.1:6333"), ("cloud", "https://cluster.example")]
)
def test_remote_backend_client_contract(mode, url, monkeypatch):
    """Verify remote backend client contract."""
    factory = Mock()
    monkeypatch.setattr("qdrant_client.QdrantClient", factory)
    cfg = config(qdrant={"mode": mode, "url": url, "api_key": "sentinel", "prefer_grpc": True})
    build_client(cfg)
    factory.assert_called_once_with(url=url, api_key="sentinel", timeout=5, prefer_grpc=True)


def test_collection_partition_keeps_failed_writes_from_other_targets(tmp_path):
    """Verify collection partition keeps failed writes from other targets."""
    from qdrant_memory.ledger import Ledger

    a, b = Ledger(tmp_path, "a"), Ledger(tmp_path, "b")
    key = a.enqueue_operation("one", "DELETE", {}, source_version="one")
    a.failure("operations", key, ValueError("bad"), terminal=True)
    b.retry_failed()
    assert a.row("operations", key)["status"] == "FAILED"
    a.close()
    b.close()


def test_ledger_destination_namespace_changes_with_endpoint():
    """Verify ledger destination namespace changes with endpoint."""
    from qdrant_memory.config import ledger_namespace

    a = config(qdrant={"mode": "server", "url": "http://one.example"})
    b = config(qdrant={"mode": "server", "url": "http://two.example"})
    assert ledger_namespace(a) != ledger_namespace(b)
