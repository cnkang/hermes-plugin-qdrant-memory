"""Logical-device grants against a disposable real Qdrant Server, never a mock."""

import os
import uuid

import pytest
from qdrant_client import QdrantClient, models

from qdrant_memory.shared_service import SharedConflictError, SharedOwner

from ..helpers import config, runtime
from ..standalone.test_device_grants import grant
from .test_cloud_owner import _exercise_cloud_owner


def test_cloud_owner_contracts_on_strict_server(tmp_path):
    """Reproduce Cloud's indexed-filter requirement without Cloud credentials."""
    url = os.environ.get("QDRANT_DEVICE_FIXTURE_URL")
    if not url:
        pytest.skip("SKIPPED: explicit disposable local Qdrant Server fixture URL required")
    from qdrant_memory.shared_protocol import validate_loopback_url

    validate_loopback_url(url)
    collection = "qm_strict_owner_" + uuid.uuid4().hex
    cfg = config(qdrant={"mode": "server", "url": url, "collection": collection})
    rt = runtime(tmp_path, client=QdrantClient(url=url), cfg=cfg)
    client = rt.store.client
    rt.ledger.close()
    try:
        client.update_collection(
            collection,
            strict_mode_config=models.StrictModeConfig(
                enabled=True,
                unindexed_filtering_retrieve=False,
                unindexed_filtering_update=False,
            ),
        )
        _exercise_cloud_owner(
            tmp_path / "owner",
            client,
            {
                "url": url,
                "owner_collection": collection,
            },
        )
    finally:
        client.delete_collection(collection)
        client.close()


def test_device_private_identity_cas_and_restart_on_real_server(tmp_path):
    """Keep backend validation distinct from the separate HTTPS network acceptance."""
    url = os.environ.get("QDRANT_DEVICE_FIXTURE_URL")
    if not url:
        pytest.skip("SKIPPED: explicit disposable local Qdrant Server fixture URL required")
    from qdrant_memory.shared_protocol import validate_loopback_url

    validate_loopback_url(url)
    cfg = config(
        qdrant={"mode": "server", "url": url, "collection": "qm_device_" + uuid.uuid4().hex}
    )
    tokens = ["a" * 40, "b" * 40, "c" * 40]
    grants = [
        grant(tokens[0], "a"),
        grant(tokens[1], "b"),
        grant(tokens[2], "c", logical="codex", space="codex-private"),
    ]
    rt = runtime(tmp_path, client=QdrantClient(url=url), cfg=cfg)
    try:
        owner = SharedOwner(rt, grants)
        saved = owner.call(
            tokens[0], "qdrant_memory_add", {"text": "cats private", "request_id": "a"}
        )
        assert (
            owner.call(tokens[1], "qdrant_memory_search", {"query": "cats"})["memories"][0]["id"]
            == saved["id"]
        )
        assert not owner.call(tokens[2], "qdrant_memory_search", {"query": "cats"})["memories"]
        update = {
            "id": saved["id"],
            "text": "cats revised",
            "expected_version": saved["version"],
            "request_id": "b",
        }
        changed = owner.call(tokens[1], "qdrant_memory_update", update)
        with pytest.raises(SharedConflictError):
            owner.call(tokens[0], "qdrant_memory_update", {**update, "request_id": "stale"})
        rt.ledger.close()
        rt.store.close()
        rt = runtime(tmp_path, client=QdrantClient(url=url), cfg=cfg)
        owner = SharedOwner(rt, grants)
        assert owner.call(tokens[1], "qdrant_memory_update", update) == changed
        rows = owner.call(tokens[0], "qdrant_memory_search", {"query": "cats"})["memories"]
        assert rows[0]["updated_by_device"] == "b"
    finally:
        rt.store.client.delete_collection(rt.store.collection)
        rt.ledger.close()
        rt.store.close()
