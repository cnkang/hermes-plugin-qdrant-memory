"""Credentialed L2 Owner contracts on an explicitly provisioned disposable collection."""

import json
import uuid
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

from qdrant_memory.config import validate_url
from qdrant_memory.extraction import Extractor
from qdrant_memory.ledger import Ledger
from qdrant_memory.qdrant_store import QdrantStore
from qdrant_memory.runtime import Runtime
from qdrant_memory.shared_security import private_entry
from qdrant_memory.shared_service import SharedConflictError, SharedOwner

from ..helpers import LLM, Embedder, config
from ..standalone.test_device_grants import grant


def test_scoped_cloud_owner_devices_cas_and_recovery(tmp_path, request):
    """Database credentials stay in the Owner; clients carry independent grants only."""
    filename = request.config.getoption("--qdrant-cloud-config")
    if not filename:
        pytest.skip("SKIPPED: explicitly authorized disposable Cloud Owner configuration required")
    path = private_entry(Path(filename))
    settings = json.loads(path.read_text())
    collection = settings.get("owner_collection", "")
    key = settings.get("owner_api_key")
    if not key or not collection:
        pytest.skip("SKIPPED: separate scoped Owner key and pre-provisioned collection required")
    if not collection.startswith("qm_owner_ci_"):
        raise ValueError("Cloud Owner acceptance requires a disposable qm_owner_ci_ collection")
    validate_url(settings["url"], cloud=True, api_key=key)
    client = QdrantClient(url=settings["url"], api_key=key, trust_env=False)
    try:
        with pytest.raises(UnexpectedResponse) as denied:
            client.get_collection("qm_owner_forbidden_" + uuid.uuid4().hex)
        assert denied.value.status_code == 403
        _exercise_cloud_owner(tmp_path, client, settings)
    finally:
        client.close()


def test_cloud_owner_disposable_collection(tmp_path, request):
    """Run authorized Owner contracts with a test key and a disposable collection."""
    filename = request.config.getoption("--qdrant-cloud-config")
    if not filename:
        pytest.skip("SKIPPED: explicitly authorized Cloud test configuration required")
    path = private_entry(Path(filename))
    settings = json.loads(path.read_text())
    key = settings.get("test_api_key")
    if not key:
        pytest.skip("SKIPPED: explicit test-key lane was not selected")
    validate_url(settings["url"], cloud=True, api_key=key)
    # Always generate a fresh collection. Never reuse or delete an operator's
    # existing collection in this lane, even if a collection name was supplied.
    collection = "qm_owner_ci_" + uuid.uuid4().hex
    settings = {**settings, "owner_collection": collection}
    cfg = config(qdrant={"mode": "cloud", "url": settings["url"], "collection": collection})
    client = QdrantClient(url=settings["url"], api_key=key, trust_env=False)
    provision_started = False
    try:
        assert not client.collection_exists(collection), "Disposable collection already exists"
        provision_started = True
        # Retain the exact fresh name for an independent always() cleanup step
        # if the test process exits before its finally block can run.
        path.write_text(json.dumps({**settings, "provision_started": True}))
        QdrantStore(client, cfg, Embedder()).initialize(create=True)
        print("Cloud Owner test-key lane: database credential scope is not certified.")
        _exercise_cloud_owner(tmp_path, client, settings)
    finally:
        try:
            if provision_started:
                if client.collection_exists(collection):
                    client.delete_collection(collection)
                assert not client.collection_exists(collection), (
                    "Cloud test collection cleanup failed"
                )
                print("Cloud Owner collection cleanup verified: disposable collection is absent.")
        finally:
            client.close()


def _exercise_cloud_owner(tmp_path, client, settings):
    """Exercise Owner isolation, CAS and recovery on an initialized Cloud collection."""
    collection = settings["owner_collection"]
    cfg = config(qdrant={"mode": "cloud", "url": settings["url"], "collection": collection})
    rt = None
    owner_id = "fixture-" + uuid.uuid4().hex
    cleanup_filter = Filter(must=[FieldCondition(key="user_id", match=MatchValue(value=owner_id))])
    initialized = False
    try:
        client.get_collection(collection)
        store = QdrantStore(client, cfg, Embedder())
        # Provisioning and fingerprint enrollment belong to a separate trusted
        # operator step. The Owner lane must never create or delete collections.
        store.initialize(create=False)
        initialized = True
        rt = Runtime(cfg, store, Ledger(tmp_path, collection), Extractor(LLM(), cfg["llm"]))
        a, b, other = [uuid.uuid4().hex + uuid.uuid4().hex for _ in range(3)]
        clients = [
            grant(a, "device-a", owner_id=owner_id),
            grant(b, "device-b", owner_id=owner_id),
            grant(other, "codex", logical="codex", space="codex-private", owner_id=owner_id),
        ]
        project_a, project_b = [uuid.uuid4().hex + uuid.uuid4().hex for _ in range(2)]
        clients += [
            grant(project_a, "project-a", owner_id=owner_id, space="alpha", visibility="project"),
            grant(
                project_b,
                "project-b",
                logical="codex",
                owner_id=owner_id,
                space="alpha",
                visibility="project",
            ),
        ]
        owner = SharedOwner(rt, clients)
        project = owner.call(
            project_a,
            "qdrant_memory_add",
            {"text": "cats synthetic shared project", "request_id": "project"},
        )
        assert (
            owner.call(project_b, "qdrant_memory_search", {"query": "cats"})["memories"][0]["id"]
            == project["id"]
        )
        project_update = {
            "id": project["id"],
            "text": "cats synthetic project revision",
            "expected_version": project["version"],
            "request_id": "project-update",
        }
        project_changed = owner.call(project_b, "qdrant_memory_update", project_update)
        with pytest.raises(SharedConflictError):
            owner.call(
                project_a, "qdrant_memory_update", {**project_update, "request_id": "project-stale"}
            )
        saved = owner.call(
            a, "qdrant_memory_add", {"text": "cats synthetic Cloud decision", "request_id": "one"}
        )
        assert (
            owner.call(b, "qdrant_memory_search", {"query": "cats"})["memories"][0]["id"]
            == saved["id"]
        )
        assert owner.call(other, "qdrant_memory_search", {"query": "cats"})["memories"] == []
        update = {
            "id": saved["id"],
            "text": "cats synthetic revision",
            "expected_version": saved["version"],
            "request_id": "two",
        }
        revised = owner.call(b, "qdrant_memory_update", update)
        with pytest.raises(SharedConflictError):
            owner.call(a, "qdrant_memory_update", {**update, "request_id": "stale"})
        rt.ledger.close()
        rt = Runtime(cfg, store, Ledger(tmp_path, collection), Extractor(LLM(), cfg["llm"]))
        owner = SharedOwner(rt, clients)
        assert owner.call(b, "qdrant_memory_update", update) == revised
        assert owner.call(project_b, "qdrant_memory_update", project_update) == project_changed
    finally:
        try:
            if initialized:
                # Use the invocation's unique Owner scope, including writes whose
                # responses were lost. Keep other Owners and the identity marker.
                client.delete(
                    collection, points_selector=FilterSelector(filter=cleanup_filter), wait=True
                )
                assert (
                    client.count(collection, count_filter=cleanup_filter, exact=True).count == 0
                ), "Cloud Owner cleanup left synthetic records"
                print("Cloud Owner cleanup verified: zero records remain for this invocation.")
        finally:
            if rt is not None:
                rt.ledger.close()
