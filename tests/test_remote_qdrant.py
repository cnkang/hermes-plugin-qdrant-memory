"""Live REST/gRPC server contracts; optional Cloud runs require scoped secrets."""

import json
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from qdrant_memory.config import load_config
from qdrant_memory.migration import migrate, read_qdrant_records, verify_collection, verify_manifest
from qdrant_memory.models import Scope
from qdrant_memory.qdrant_store import INDEXES, build_client
from qdrant_memory.tools import dispatch

from .helpers import config, runtime


def wait_for_server(cfg):
    """Wait for an actual server startup without treating an outage as a skip."""
    deadline = time.monotonic() + 30
    while True:
        client = build_client(cfg)
        try:
            client.get_collections()
            return
        except Exception:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)
        finally:
            client.close()


@pytest.mark.parametrize("prefer_grpc", [False, True])
def test_remote_server_contract_and_restart(tmp_path, prefer_grpc, request):
    """Exercise indexes, scope, mutation, restart, verification and migration remotely."""
    url = request.config.getoption("--qdrant-test-url")
    if not url:
        pytest.skip("--qdrant-test-url is required for the live server lane")
    exercise_remote(
        tmp_path,
        url,
        None,
        prefer_grpc,
        "server",
        request.config.getoption("--qdrant-test-container"),
    )


def test_cloud_contract_when_credentials_are_available(tmp_path, request):
    """Run Cloud only with an explicit endpoint and API key supplied by CI secrets."""
    config_path = request.config.getoption("--qdrant-cloud-config")
    if not config_path:
        pytest.skip("Cloud smoke is secret gated and outside the required scan gate")
    settings = json.loads(Path(config_path).read_text())
    url, key = settings["url"], settings["api_key"]
    exercise_remote(
        tmp_path, url, key, False, "cloud", None, settings.get("collection_prefix", "test_")
    )


def exercise_remote(home, url, key, prefer_grpc, mode, container, collection_prefix="test_"):
    """Use a unique disposable collection and retain no changes after the exercise."""
    cfg = load_config(
        home,
        config(
            qdrant={
                "mode": mode,
                "url": url,
                "api_key": key,
                "prefer_grpc": prefer_grpc,
                "collection": collection_prefix + uuid.uuid4().hex,
            }
        ),
    )
    wait_for_server(cfg)
    rt = runtime(home, cfg=cfg, client=build_client(cfg))
    try:
        assert set(INDEXES) <= set(
            rt.store.client.get_collection(rt.store.collection).payload_schema
        )
        alice, bob = Scope("alice", None), Scope("bob", "hermes")
        first = rt.add("cats preferred", alice)
        rt.add("cats preferred", bob)
        assert rt.store.count() == 2
        assert [hit.id for hit in rt.store.search("cats", alice)] == [first["id"]]
        assert rt.store.get(first["id"], bob) is None
        dispatch(
            rt,
            "qdrant_memory_update",
            {"id": first["id"], "text": "dogs preferred"},
            alice,
            "remote",
        )
        assert rt.store.get(first["id"], alice).payload["text"] == "dogs preferred"
        dispatch(rt, "qdrant_memory_delete", {"id": first["id"]}, alice, "remote")
        assert rt.store.count(alice) == 0
        records = [
            {
                "id": "legacy",
                "_cloud_memory_id": "cloud-id",
                "memory": "cats migration",
                "user_id": "alice",
                "agent_id": None,
            }
        ]
        manifest = migrate(rt, records, "mem0-json", "remote-fixture", verify=True)
        assert manifest["added"] == 1
        assert verify_collection(rt.store)["ok"]
        assert rt.store.count() == 2
        exercise_readonly_source(rt)
        rt.store.close()
        rt.ledger.close()
        if container:
            subprocess.run(
                ["docker", "restart", container], check=True, capture_output=True, timeout=30
            )
        wait_for_server(cfg)
        rt = runtime(home, cfg=cfg, client=build_client(cfg))
        assert rt.store.count() == 3
        assert verify_collection(rt.store)["ok"]
        assert (
            migrate(rt, records, "mem0-json", "remote-fixture", resume=True, verify=True)[
                "processed"
            ]
            == 1
        )
        exercise_fenced_migration(rt)
    finally:
        rt.store.client.delete_collection(rt.store.collection)
        rt.store.close()
        rt.ledger.close()


def exercise_fenced_migration(rt):
    """A failed import stays deleted across replay while a fresh import is allowed."""
    scope = Scope(**rt.cfg["scope"])
    other = Scope("remote-other-user", scope.agent_id)
    records = [
        {
            "id": "fenced-import",
            "memory": "dogs fenced migration",
            "user_id": scope.user_id,
            "agent_id": scope.agent_id,
            "updated_at": "2999-01-01T00:00:00Z",
        }
    ]
    upsert = rt.store.upsert

    def fail_write(_records):
        """Fail after durable admission without contacting the remote service."""
        raise ValueError("synthetic disposable write failure")

    rt.store.upsert = fail_write
    try:
        with pytest.raises(ValueError, match="synthetic disposable"):
            migrate(rt, records, "mem0-json", "remote-fenced-fixture")
    finally:
        rt.store.upsert = upsert
    manifest = rt.ledger.manifests()[-1]
    identifier, record = next(iter(manifest["records"].items()))
    old_key = record["operation_key"]
    value = json.loads(rt.ledger.row("operations", old_key)["payload_json"])
    independent_id = str(uuid.uuid4())
    rt.store.upsert([(independent_id, value)])
    isolated = rt.add(value["text"], other)
    dispatch(rt, "qdrant_memory_delete", {"id": independent_id}, scope, "remote")
    resumed = migrate(
        rt, records, "mem0-json", "remote-fenced-fixture", resume=True, retry_failed=True
    )
    assert resumed["completed_at"] is not None
    assert resumed["superseded"] == resumed["processed"] == 1
    assert resumed["applied"] == 0
    assert rt.ledger.row("operations", old_key)["status"] == "SUPERSEDED"
    assert not verify_manifest(rt.store, resumed)["ok"]
    assert rt.recover() == 0
    assert rt.store.get(identifier, scope) is None
    assert rt.store.get(isolated["id"], other) is not None
    fresh = migrate(rt, records, "mem0-json", "remote-fenced-fixture", verify=True)
    assert fresh["migration_id"] != resumed["migration_id"]
    assert fresh["applied"] == 1
    assert verify_manifest(rt.store, fresh)["ok"]
    recovery_id = str(uuid.uuid4())
    recovery_key = rt.operation(recovery_id, "UPSERT", value)
    rt.store.upsert = fail_write
    try:
        with pytest.raises(ValueError, match="synthetic disposable"):
            rt.commit([recovery_key])
    finally:
        rt.store.upsert = upsert
    assert rt.ledger.row("operations", recovery_key)["status"] == "FAILED"
    rt.ledger.retry_failed()
    assert rt.recover() == 0
    assert rt.ledger.row("operations", recovery_key)["status"] == "COMMITTED"
    assert rt.store.get(recovery_id, scope) is not None


def exercise_readonly_source(rt):
    """Import a real remote legacy collection while preserving every source field."""
    from qdrant_client import models

    client = rt.store.client
    collection = "source_" + uuid.uuid4().hex
    identifier = str(uuid.uuid4())
    client.create_collection(
        collection, vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE)
    )
    try:
        client.upsert(
            collection,
            [
                models.PointStruct(
                    id=identifier,
                    vector=[1, 0, 0],
                    payload={"memory": "cats source", "user_id": "alice", "agent_id": None},
                )
            ],
            wait=True,
        )
        before = client.retrieve(collection, [identifier], with_payload=True, with_vectors=True)
        records = list(read_qdrant_records(client, collection))
        assert migrate(rt, records, "mem0-qdrant", collection, verify=True)["added"] == 1
        after = client.retrieve(collection, [identifier], with_payload=True, with_vectors=True)
        assert after == before
        assert client.count(collection, exact=True).count == 1
    finally:
        client.delete_collection(collection)
