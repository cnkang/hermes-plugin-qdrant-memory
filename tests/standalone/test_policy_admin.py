"""Offline credential administration is atomic, scoped and safe to repeat."""

import pytest

from qdrant_memory.policy_admin import revoke_device, rotate_device
from qdrant_memory.shared_service import load_policy

from .test_device_grants import grant, write_policy


def test_revoke_repeat_and_rotation_preserve_other_devices(tmp_path):
    """Losing one device does not revoke another device of the same logical agent."""
    policy = tmp_path / "policy.json"
    write_policy(policy, [grant("a" * 40, "mac"), grant("b" * 40, "linux")])
    assert revoke_device(policy, "alice", "mac")["changed"] == 1
    assert revoke_device(policy, "alice", "mac")["changed"] == 0
    assert not load_policy(policy)[1].get("revoked")
    output = tmp_path / "device.token"
    assert rotate_device(policy, "alice", "linux", output)["changed"] == 1
    assert len(output.read_text().strip()) >= 32
    assert load_policy(policy)[0]["revoked"] is True
    assert output.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        rotate_device(policy, "alice", "linux", output)


def test_unknown_device_and_symlink_fail_without_mutation(tmp_path):
    """Reject ambiguous paths and unknown identities instead of repairing them."""
    policy = tmp_path / "policy.json"
    write_policy(policy, [grant("a" * 40, "mac")])
    original = policy.read_bytes()
    with pytest.raises(ValueError):
        revoke_device(policy, "other-owner", "mac")
    assert policy.read_bytes() == original
    alias = tmp_path / "alias.json"
    alias.symlink_to(policy)
    with pytest.raises(ValueError):
        revoke_device(alias, "alice", "mac")
