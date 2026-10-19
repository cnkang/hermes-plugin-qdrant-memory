"""Explicit opt-in database isolation gate using a disposable collection only."""

import json
import os
from contextlib import closing
from threading import Thread

import pytest

from ..standalone.test_standalone import profile as standalone_profile


@pytest.fixture
def profile(tmp_path):
    """Keep the live collection's embedding service local and deterministic."""
    yield from standalone_profile.__wrapped__(tmp_path)


@pytest.mark.skipif(
    os.environ.get("QDRANT_MEMORY_SECURITY_LIVE") != "1",
    reason="UNVERIFIED: disposable authenticated Qdrant isolation deployment not supplied",
)
def test_database_collection_write_isolation(profile):
    """Require owner success and explicit denial for absent, read-only and unrelated keys."""
    from qdrant_client import QdrantClient, models
    from qdrant_client.http.exceptions import UnexpectedResponse

    url = os.environ["QDRANT_MEMORY_SECURITY_URL"]
    collection = os.environ["QDRANT_MEMORY_SECURITY_COLLECTION"]
    owner_key = os.environ["QDRANT_MEMORY_SECURITY_OWNER_KEY"]
    read_key = os.environ["QDRANT_MEMORY_SECURITY_READ_KEY"]
    unrelated_key = os.environ["QDRANT_MEMORY_SECURITY_UNRELATED_KEY"]
    bootstrap_key = os.environ["QDRANT_MEMORY_SECURITY_BOOTSTRAP_KEY"]
    assert url.startswith("https://"), "Use an authenticated TLS test endpoint"
    assert collection.startswith("qm_security_"), "Authorize a disposable qm_security_ collection"
    with (
        closing(QdrantClient(url=url, api_key=bootstrap_key)) as bootstrap,
        closing(QdrantClient(url=url, api_key=owner_key)) as owner,
    ):
        assert not bootstrap.collection_exists(collection), "Never reuse an existing collection"
        bootstrap.create_collection(
            collection,
            vectors_config={"dense": models.VectorParams(size=3, distance=models.Distance.COSINE)},
        )
        try:
            from qdrant_memory.config import ledger_namespace
            from qdrant_memory.embedding import build_embedder
            from qdrant_memory.ledger import Ledger
            from qdrant_memory.qdrant_store import QdrantStore
            from qdrant_memory.runtime import Runtime
            from qdrant_memory.shared_client import call
            from qdrant_memory.shared_service import SharedOwner, load_policy, make_server
            from qdrant_memory.standalone import load_standalone_config

            from ..standalone.test_shared_service import TOKENS, policy

            home, _ = profile
            cfg = load_standalone_config(home)
            cfg["qdrant"].update(mode="server", url=url, api_key=owner_key, collection=collection)
            with (
                closing(build_embedder(None, cfg, secret_reader=os.environ.get)) as embedder,
                closing(Ledger(home, ledger_namespace(cfg))) as ledger,
            ):
                store = QdrantStore(owner, cfg, embedder)
                store.initialize(create=True)
                service = SharedOwner(Runtime(cfg, store, ledger), load_policy(policy(home)))
                with make_server(service, 0) as server:
                    thread = Thread(target=server.serve_forever)
                    thread.start()
                    try:
                        result = call(
                            f"http://127.0.0.1:{server.server_port}",
                            TOKENS["hermes"],
                            "qdrant_memory_add",
                            {"text": "disposable isolation fixture", "request_id": "isolation"},
                        )
                        assert result["action"] == "ADD"
                    finally:
                        server.shutdown()
                        thread.join(timeout=10)
            point = models.PointStruct(
                id=1, vector={"dense": [1.0, 0.0, 0.0]}, payload={"fixture": True}
            )
            owner.upsert(collection, points=[point], wait=True)
            with closing(QdrantClient(url=url, api_key=read_key)) as reader:
                assert reader.retrieve(collection, ids=[1])
            for credential in (None, read_key, unrelated_key):
                with closing(QdrantClient(url=url, api_key=credential)) as denied:
                    with pytest.raises(UnexpectedResponse) as caught:
                        denied.upsert(collection, points=[point], wait=True)
                    assert caught.value.status_code in {401, 403}, (
                        "Require authorization denial, not a transport failure"
                    )
            assert owner.count(collection, exact=True).count == 3
            from qdrant_memory.shared_security import doctor

            public_config = {
                **cfg,
                "qdrant": {
                    **cfg["qdrant"],
                    "api_key": None,
                    "api_key_env": "QDRANT_MEMORY_SECURITY_OWNER_KEY",
                },
            }
            config_path = home / "qdrant-memory.json"
            config_path.write_text(json.dumps(public_config))
            config_path.chmod(0o600)
            home.chmod(0o700)
            report = doctor(home, home / "shared-policy.json")
            results = {item["code"]: item["status"] for item in report["checks"]}
            assert results["database_schema"] == "PASS"
            assert results["credential_isolation"] == "UNVERIFIED"
            assert owner.count(collection, exact=True).count == 3
        finally:
            bootstrap.delete_collection(collection)
