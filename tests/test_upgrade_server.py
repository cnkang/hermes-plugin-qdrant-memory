"""Live-server policy adoption: missing, unmanaged and conflicting indexes."""

import uuid

import pytest
from qdrant_client import models as m

from qdrant_memory import strategy, upgrade
from qdrant_memory.qdrant_store import IDENTITY_ID, build_client

from .helpers import config
from .test_remote_qdrant import wait_for_server
from .test_upgrade_policy import plan_args


def server_cfg(url, collection):
    """Return a live-server configuration for one disposable collection."""
    return config(
        qdrant={
            "mode": "server",
            "url": url,
            "collection": collection,
            "timeout_seconds": 15,
        }
    )


def create_legacy_collection(cfg, *, indexes=None, conflict=None):
    """Create a 0.6.x-shaped collection with an identity point and one memory."""
    client = build_client(cfg)
    target = strategy.desired_policy(cfg)
    collection = cfg["qdrant"]["collection"]
    try:
        client.create_collection(
            collection,
            vectors_config={
                "dense": m.VectorParams(size=target["dimensions"], distance=m.Distance.COSINE)
            },
        )
        for field, kind in (indexes or {}).items():
            client.create_payload_index(collection, field, field_schema=kind, wait=True)
        if conflict:
            client.create_payload_index(collection, conflict, field_schema="integer", wait=True)
        client.upsert(
            collection,
            points=[
                m.PointStruct(
                    id=IDENTITY_ID,
                    vector={"dense": [0.0] * target["dimensions"]},
                    payload={
                        "_qdrant_memory_schema": 1,
                        "embedding_fingerprint": target["embedding_fingerprint"],
                    },
                ),
                m.PointStruct(
                    id=str(uuid.uuid4()),
                    vector={"dense": [1.0, 0.0, 0.0]},
                    payload={
                        "user_id": "alice",
                        "agent_id": None,
                        "text": "cats preferred",
                        "content_hash": "sha256:fixture",
                    },
                ),
            ],
            wait=True,
        )
    finally:
        client.close()


def drop(cfg):
    """Remove the disposable collection."""
    client = build_client(cfg)
    try:
        client.delete_collection(cfg["qdrant"]["collection"])
    except Exception:
        client.close()
        return
    client.close()


def payload_fields(cfg):
    """Return the collection's actual payload index field names."""
    client = build_client(cfg)
    try:
        return set(client.get_collection(cfg["qdrant"]["collection"]).payload_schema)
    finally:
        client.close()


def remote_apply(result):
    """Return the operator arguments a remote apply requires."""
    return plan_args(
        upgrade_action="apply",
        plan=result["plan_id"],
        confirm_writers_isolated=True,
        accept_operator_backup=True,
    )


@pytest.fixture
def live(request):
    """Skip when the live server lane is not configured."""
    url = request.config.getoption("--qdrant-test-url")
    if not url:
        pytest.skip("--qdrant-test-url is required for the live server lane")
    return url


def test_plan_reports_missing_without_creating_them(live, tmp_path):
    """Planning is read-only: missing indexes stay missing until an apply."""
    cfg = server_cfg(live, "upgrade_plan_" + uuid.uuid4().hex[:8])
    wait_for_server(cfg)
    create_legacy_collection(cfg)
    try:
        result = upgrade.describe(tmp_path, cfg, plan_args())
        assert result["blockers"] == []
        assert result["indexes"]["status"] == strategy.MISSING
        assert payload_fields(cfg) == set()
        applied = upgrade.describe(tmp_path, cfg, remote_apply(result))
        assert applied["phase"] == upgrade.PHASE_READY
        assert set(applied["steps"]["indexes"]["created"]) == set(
            strategy.index_policy()["indexes"]
        )
        assert set(strategy.index_policy()["indexes"]) <= payload_fields(cfg)
        assert applied["steps"]["verify"]["scope_probe"] == 1
        assert applied["steps"]["backup"]["outcome"] == "operator-required"
    finally:
        drop(cfg)


def test_extra_custom_index_is_reported_and_preserved(live, tmp_path):
    """An operator index outside the policy is visible but never removed."""
    cfg = server_cfg(live, "upgrade_extra_" + uuid.uuid4().hex[:8])
    wait_for_server(cfg)
    create_legacy_collection(cfg, indexes={"custom_tag": "keyword"})
    try:
        result = upgrade.describe(tmp_path, cfg, plan_args())
        assert [item["field"] for item in result["indexes"]["extra"]] == ["custom_tag"]
        upgrade.describe(tmp_path, cfg, remote_apply(result))
        assert "custom_tag" in payload_fields(cfg)
    finally:
        drop(cfg)


def test_conflicting_index_blocks_instead_of_rebuilding(live, tmp_path):
    """A conflicting type is a blocker; this release never replaces it."""
    cfg = server_cfg(live, "upgrade_conflict_" + uuid.uuid4().hex[:8])
    wait_for_server(cfg)
    create_legacy_collection(cfg, conflict="user_id")
    try:
        result = upgrade.describe(tmp_path, cfg, plan_args())
        assert result["ok"] is False
        assert result["indexes"]["status"] == strategy.CONFLICT
        conflicted = [
            entry["field"]
            for entry in result["indexes"]["entries"]
            if entry["status"] == strategy.CONFLICT
        ]
        assert conflicted == ["user_id"]
        with pytest.raises(upgrade.UpgradeBlockedError):
            upgrade.describe(tmp_path, cfg, remote_apply(result))
    finally:
        drop(cfg)


def test_server_version_and_strict_mode_are_reported(live, tmp_path):
    """A reachable server reports its own version and strict-mode setting."""
    cfg = server_cfg(live, "upgrade_probe_" + uuid.uuid4().hex[:8])
    wait_for_server(cfg)
    create_legacy_collection(cfg)
    try:
        result = upgrade.describe(tmp_path, cfg, plan_args(dry_run=True))
        assert result["backend_version"]["source"] == "server-info"
        assert result["backend_version"]["version"].startswith("1.")
        assert result["strict_mode"] in {"enabled", "disabled"}
        assert result["persisted"] is False
        assert not (tmp_path / "qdrant-memory" / "strategy").exists()
    finally:
        drop(cfg)
