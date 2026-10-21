"""Exercise Cloud fixture cleanup with a real local Qdrant store."""

import json
import uuid
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import PointStruct

from ..helpers import Embedder, config
from . import test_cloud_owner as cloud


@pytest.mark.parametrize("delete_works", [True, False])
def test_cleanup_after_write_response_is_lost(tmp_path, monkeypatch, delete_works):
    """Remove committed synthetic data even if its response never reaches the test."""
    client = QdrantClient(":memory:")
    collection = "qm_owner_ci_cleanup"
    cfg = config(qdrant={"collection": collection})
    cloud.QdrantStore(client, cfg, Embedder()).initialize()
    unrelated_id = str(uuid.uuid4())
    client.upsert(
        collection,
        [
            PointStruct(
                id=unrelated_id,
                vector={"dense": [1.0, 0.0, 0.0]},
                payload={"user_id": "unrelated-owner", "text": "synthetic unrelated record"},
            )
        ],
    )
    get_collection = client.get_collection

    def scoped_get_collection(name):
        if name != collection:
            raise UnexpectedResponse(403, "Forbidden", b"{}", {})
        return get_collection(name)

    monkeypatch.setattr(client, "get_collection", scoped_get_collection)
    monkeypatch.setattr(cloud, "QdrantClient", lambda **kwargs: client)
    close = client.close
    monkeypatch.setattr(client, "close", lambda: None)
    call = cloud.SharedOwner.call

    def lose_response(self, *args, **kwargs):
        call(self, *args, **kwargs)
        raise RuntimeError("synthetic response lost after commit")

    monkeypatch.setattr(cloud.SharedOwner, "call", lose_response)
    if not delete_works:
        monkeypatch.setattr(client, "delete", lambda *args, **kwargs: None)
    filename = tmp_path / "cloud.json"
    filename.write_text(
        json.dumps(
            {
                "url": "https://fixture.cloud.qdrant.io",
                "owner_api_key": "synthetic-key",
                "owner_collection": collection,
            }
        )
    )
    filename.chmod(0o600)
    request = SimpleNamespace(config=SimpleNamespace(getoption=lambda name: str(filename)))
    try:
        error, message = (
            (RuntimeError, "response lost")
            if delete_works
            else (AssertionError, "cleanup left synthetic records")
        )
        with pytest.raises(error, match=message):
            cloud.test_scoped_cloud_owner_devices_cas_and_recovery(tmp_path, request)
        # The identity marker stays; the unacknowledged memory must not stay.
        assert client.count(collection, exact=True).count == (2 if delete_works else 3)
        assert client.retrieve(collection, [unrelated_id])
    finally:
        close()
