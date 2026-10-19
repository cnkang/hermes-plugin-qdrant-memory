"""POSIX administrator-only, atomic device credential maintenance."""

import hashlib
import json
import os
import secrets
import tempfile
from contextlib import contextmanager
from pathlib import Path

import portalocker

from .private_paths import checked_absolute_path
from .shared_security import private_entry
from .shared_service import load_policy_document


@contextmanager
def _policy_lock(path):
    """Serialize cooperating admins without following a substituted lock file."""
    path = checked_absolute_path(Path(path).absolute())
    private_entry(path)
    private_entry(path.parent, directory=True)
    lock_path = path.with_name(path.name + ".admin.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as stream:
        private_entry(lock_path)
        portalocker.lock(stream, portalocker.LOCK_EX)
        try:
            yield path, load_policy_document(path)
        finally:
            portalocker.unlock(stream)


def _replace(path, policy):
    """Validate a private temporary policy before durable atomic publication."""
    descriptor, temporary = tempfile.mkstemp(prefix=".policy-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(policy, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        load_policy_document(temporary)
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _devices(policy, owner_id, device_id):
    matches = [
        grant
        for grant in policy["clients"]
        if grant["owner_id"] == owner_id and grant.get("device_id") == device_id
    ]
    if not matches:
        raise ValueError("Unknown owner/device binding")
    return matches


def revoke_device(path, owner_id, device_id):
    """Revoke all space grants of one device, safely repeatable after device loss."""
    with _policy_lock(path) as (path, policy):
        changed = 0
        for grant in _devices(policy, owner_id, device_id):
            changed += not grant.get("revoked", False)
            grant["revoked"] = True
        if changed:
            _replace(path, policy)
        return {"ok": True, "changed": changed}


def rotate_device(path, owner_id, device_id, output):
    """Write a new private token file before replacing a single active device grant."""
    with _policy_lock(path) as (path, policy):
        matches = _devices(policy, owner_id, device_id)
        if len(matches) != 1 or matches[0].get("revoked", False):
            raise ValueError("Rotation requires exactly one active device grant")
        output = checked_absolute_path(Path(output).absolute())
        private_entry(output.parent, directory=True)
        token = secrets.token_urlsafe(48)
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        matches[0]["token_sha256"] = hashlib.sha256(token.encode()).hexdigest()
        _replace(path, policy)
        return {"ok": True, "changed": 1, "token_file": str(output)}
