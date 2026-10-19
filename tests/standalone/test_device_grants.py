"""Independent device credentials share only an administrator-bound logical identity."""

import hashlib
import json
import time

import pytest

from qdrant_memory.shared_service import SharedOwner, load_policy

from ..helpers import runtime


def grant(token, device, *, logical="hermes", space="hermes-private", **extra):
    """Build a synthetic device grant, never a production credential."""
    return {
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "principal": device,
        "owner_id": "alice",
        "space_id": space,
        "permission": "write",
        "visibility": "private",
        "logical_agent_id": logical,
        "device_id": device,
        **extra,
    }


def write_policy(path, clients):
    """Write a private operator policy into the test directory."""
    path.write_text(json.dumps({"clients": clients}))
    path.chmod(0o600)
    return load_policy(path)


def test_same_agent_two_devices_private_cas_receipts_and_live_revocation(tmp_path):
    """Device B continues A's private space; revoking A leaves B functional."""
    a, b = "a" * 40, "b" * 40
    path = tmp_path / "policy.json"
    clients = [grant(a, "mac"), grant(b, "linux")]
    grants = write_policy(path, clients)
    rt = runtime(tmp_path)
    try:
        owner = SharedOwner(rt, grants, grant_loader=lambda: load_policy(path))
        saved = owner.call(a, "qdrant_memory_add", {"text": "cats private", "request_id": "r1"})
        rows = owner.call(b, "qdrant_memory_search", {"query": "cats"})["memories"]
        assert rows[0]["id"] == saved["id"]
        assert rows[0]["created_by_device"] == "mac"
        updated = owner.call(
            b,
            "qdrant_memory_update",
            {
                "id": saved["id"],
                "text": "cats new",
                "expected_version": saved["version"],
                "request_id": "r2",
            },
        )
        assert updated["version"] != saved["version"]
        assert (
            owner.call(
                b,
                "qdrant_memory_update",
                {
                    "id": saved["id"],
                    "text": "cats new",
                    "expected_version": saved["version"],
                    "request_id": "r2",
                },
            )
            == updated
        )
        clients[0]["revoked"] = True
        write_policy(path, clients)
        with pytest.raises(PermissionError):
            owner.call(a, "qdrant_memory_search", {"query": "cats"})
        assert owner.call(b, "qdrant_memory_search", {"query": "cats"})["memories"]
        path.write_text("broken")
        with pytest.raises(PermissionError):
            owner.call(b, "qdrant_memory_search", {"query": "cats"})
    finally:
        rt.ledger.close()
        rt.store.close()


def test_logical_impersonation_rejected_before_startup(tmp_path):
    """A same-space private grant cannot name a different logical agent."""
    path = tmp_path / "policy.json"
    with pytest.raises(ValueError):
        write_policy(path, [grant("a" * 40, "a"), grant("b" * 40, "b", logical="codex")])


def test_expired_device_denied(tmp_path):
    """Expiry is evaluated on every authentication, independently of sessions."""
    path = tmp_path / "policy.json"
    token = "a" * 40
    grants = write_policy(path, [grant(token, "a", expires_at=time.time() - 1)])
    rt = runtime(tmp_path)
    try:
        owner = SharedOwner(rt, grants)
        with pytest.raises(PermissionError):
            owner.authenticate(token)
    finally:
        rt.ledger.close()
        rt.store.close()


def test_rebinding_private_space_cannot_expose_old_logical_identity(tmp_path):
    """An administrator's new mapping does not silently migrate existing private data."""
    token = "a" * 40
    clients = [grant(token, "mac")]
    rt = runtime(tmp_path)
    try:
        owner = SharedOwner(rt, clients)
        owner.call(token, "qdrant_memory_add", {"text": "cats private", "request_id": "r1"})
        clients[0] = grant(token, "mac", logical="other-agent")
        assert owner.call(token, "qdrant_memory_search", {"query": "cats"})["memories"] == []
    finally:
        rt.ledger.close()
        rt.store.close()
