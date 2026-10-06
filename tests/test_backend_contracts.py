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


def test_server_cloud_url_alias_resolves_to_same_ledger_namespace():
    """Server and Cloud mode pointing at the same host share one ledger namespace."""
    from qdrant_memory.config import ledger_namespace

    server_cfg = config(qdrant={"mode": "server", "url": "https://example.com"})
    cloud_cfg = config(qdrant={"mode": "cloud", "url": "https://Example.com:443/"})
    assert ledger_namespace(server_cfg) == ledger_namespace(cloud_cfg)


def test_ledger_replay_across_server_cloud_alias(tmp_path):
    """Pending operations survive a server-to-cloud mode switch with the same host."""
    from qdrant_memory.config import ledger_namespace

    from .helpers import runtime

    server_cfg = config(qdrant={"mode": "server", "url": "https://host.example"})
    rt = runtime(tmp_path, cfg=server_cfg, ledger_namespace=ledger_namespace(server_cfg))
    try:
        from qdrant_memory.models import Scope, payload, point_id

        scope = Scope(**rt.cfg["scope"])
        text = "cat fact"
        identifier = point_id(scope, "test", text)
        value = payload(text, scope, "test", "s1")
        key = rt.operation(identifier, "UPSERT", value)
        assert rt.ledger.row("operations", key)["status"] == "PENDING"
    finally:
        rt.store.close()
        rt.ledger.close()

    cloud_cfg = config(qdrant={"mode": "cloud", "url": "https://Host.example:443/"})
    assert ledger_namespace(server_cfg) == ledger_namespace(cloud_cfg)
    rt2 = runtime(tmp_path, cfg=cloud_cfg, ledger_namespace=ledger_namespace(cloud_cfg))
    try:
        assert rt2.recover() == 0
        assert rt2.ledger.row("operations", key)["status"] == "COMMITTED"
        assert rt2.store.count() == 1
    finally:
        rt2.store.close()
        rt2.ledger.close()
