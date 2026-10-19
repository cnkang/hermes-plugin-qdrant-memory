"""Protected destinations reject unsafe admission before touching memory."""

import json
import os

import pytest

from qdrant_memory.config import backend_destination, embedding_config, fingerprint
from qdrant_memory.models import digest
from qdrant_memory.ownership import WriterBusyError, WriterLease
from qdrant_memory.standalone import load_standalone_config

from ..standalone.test_shared_service import policy
from ..standalone.test_standalone import command
from ..standalone.test_standalone import profile as standalone_profile


@pytest.fixture
def profile(tmp_path):
    """Reuse the deterministic local embedding transport."""
    yield from standalone_profile.__wrapped__(tmp_path)


def protected_policy(home):
    """Create an explicit operator policy for the fixture destination."""
    cfg = load_standalone_config(home)
    path = policy(home)
    data = json.loads(path.read_text())
    data["destination"] = {
        "backend": cfg["qdrant"]["mode"],
        "destination": list(backend_destination(cfg["qdrant"])),
        "collection": cfg["qdrant"]["collection"],
        "embedding_fingerprint": fingerprint(embedding_config(cfg)),
        "owner_profile": digest(str(home.resolve())),
        "schema_version": 1,
        "security_mode": "local-embedded",
    }
    path.write_text(json.dumps(data))
    return path, data["destination"]


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """Never register test destinations in the operator account's real registry."""
    monkeypatch.setattr(
        "qdrant_memory.shared_security.registry_path",
        lambda: tmp_path / "account" / ".qdrant-memory-owners",
    )


def make_private(home):
    """Use operator-private permissions for every existing state entry."""
    home.chmod(0o700)
    for path in home.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)


@pytest.mark.parametrize(
    "field",
    [
        "collection",
        "embedding_fingerprint",
        "owner_profile",
        "schema_version",
        "destination",
        "security_mode",
    ],
)
def test_policy_mismatch_precedes_storage(profile, field):
    """A malformed binding cannot initialize or recover any memory."""
    from qdrant_memory.shared_security import validate_destination

    home, _ = profile
    path, binding = protected_policy(home)
    binding[field] = "incorrect"
    make_private(home)
    before = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    with pytest.raises(ValueError):
        validate_destination(home, load_standalone_config(home), binding)
    assert sorted(str(p.relative_to(home)) for p in home.rglob("*")) == before
    assert path.exists()


@pytest.mark.skipif(os.name == "nt", reason="Protected mode requires POSIX permissions")
def test_claim_blocks_other_profile_and_private_admission(profile, tmp_path):
    """Claims survive restart and protect every cooperating lease entry point."""
    home, _ = profile
    _, binding = protected_policy(home)
    make_private(home)
    cfg = load_standalone_config(home)
    lease = WriterLease.for_config(home, cfg, destination_policy=binding)
    try:
        with pytest.raises(WriterBusyError):
            WriterLease.for_config(home, cfg, destination_policy=binding)
        with pytest.raises((PermissionError, WriterBusyError)):
            WriterLease.for_config(tmp_path / "other", cfg)
    finally:
        lease.close()
    with pytest.raises(PermissionError):
        WriterLease.for_config(home, cfg)
    reopened = WriterLease.for_config(home, cfg, destination_policy=binding)
    reopened.close()
    other = tmp_path / "other-owner"
    other.mkdir(mode=0o700)
    (other / "qdrant-memory.json").write_text(json.dumps(cfg))
    make_private(other)
    second_binding = {**binding, "owner_profile": digest(str(other.resolve()))}
    with pytest.raises(PermissionError, match="another protected owner"):
        WriterLease.for_config(other, cfg, destination_policy=second_binding)


@pytest.mark.skipif(os.name == "nt", reason="POSIX privacy checks require POSIX permissions")
@pytest.mark.parametrize("unsafe", ["home", "config", "ledger", "symlink"])
def test_unsafe_owner_state_rejected_without_repair(profile, unsafe):
    """Protected mode rejects exposed state instead of silently changing its mode."""
    from qdrant_memory.shared_security import validate_destination

    home, _ = profile
    _, binding = protected_policy(home)
    make_private(home)
    if unsafe == "home":
        target = home
        target.chmod(0o755)
    elif unsafe == "config":
        target = home / "qdrant-memory.json"
        target.chmod(0o644)
    else:
        directory = home / "qdrant-memory"
        directory.mkdir(mode=0o700)
        target = directory / "state.db"
        if unsafe == "symlink":
            target.symlink_to(home / "qdrant-memory.json")
        else:
            target.write_text("sensitive fixture")
            target.chmod(0o644)
    previous = target.lstat()
    with pytest.raises((PermissionError, ValueError)):
        validate_destination(home, load_standalone_config(home), binding)
    assert target.lstat().st_mode == previous.st_mode


@pytest.mark.skipif(os.name == "nt", reason="Protected mode requires POSIX permissions")
def test_doctor_is_non_mutating_and_redacted(profile):
    """Diagnostics neither open a write session nor expose private identifiers."""
    from qdrant_memory.shared_security import doctor

    home, _ = profile
    command(profile, "init")
    path, _ = protected_policy(home)
    make_private(home)
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    report = doctor(home, path)
    serialized = json.dumps(report)
    assert "UNVERIFIED" in serialized
    assert str(home) not in serialized
    assert "token_sha256" not in serialized
    assert before == {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    assert {c["code"] for c in report["checks"]} >= {
        "destination",
        "owner_state",
        "recovery",
        "credential_isolation",
        "backup",
    }


@pytest.mark.skipif(os.name == "nt", reason="Protected mode requires POSIX permissions")
def test_private_writer_prevents_enrollment(profile):
    """An already running private writer cannot overlap protected enrollment."""
    home, _ = profile
    _, binding = protected_policy(home)
    make_private(home)
    cfg = load_standalone_config(home)
    private = WriterLease.for_config(home, cfg)
    try:
        with pytest.raises(WriterBusyError):
            WriterLease.for_config(home, cfg, destination_policy=binding)
    finally:
        private.close()


@pytest.mark.skipif(os.name == "nt", reason="Protected mode requires POSIX permissions")
def test_protected_write_rechecks_permissions_and_preserves_receipt(profile):
    """Exposing state after startup blocks mutation before durable admission."""
    from qdrant_memory.shared_security import validate_destination
    from qdrant_memory.shared_service import SharedOwner, load_policy
    from qdrant_memory.standalone import open_runtime

    from ..standalone.test_shared_service import TOKENS

    home, _ = profile
    command(profile, "init")
    path, binding = protected_policy(home)
    make_private(home)
    previous = os.umask(0o077)
    try:
        with open_runtime(home, shared_owner=True, destination_policy=binding) as runtime:
            owner = SharedOwner(
                runtime,
                load_policy(path),
                admission_check=lambda: validate_destination(home, runtime.cfg, binding),
            )
            args = {"text": "protected shared fact", "request_id": "protected"}
            result = owner.call(TOKENS["hermes"], "qdrant_memory_add", args)
            assert owner.call(TOKENS["hermes"], "qdrant_memory_add", args) == result
            before = runtime.ledger.db.execute(
                "SELECT count(*) FROM shared_requests_v1"
            ).fetchone()[0]
            (home / "qdrant-memory.json").chmod(0o644)
            with pytest.raises(PermissionError):
                owner.call(
                    TOKENS["hermes"],
                    "qdrant_memory_add",
                    {"text": "denied", "request_id": "denied"},
                )
            assert (
                runtime.ledger.db.execute("SELECT count(*) FROM shared_requests_v1").fetchone()[0]
                == before
            )
    finally:
        os.umask(previous)


def test_doctor_cli_failure_is_structured_and_sanitized(profile):
    """Invalid policy exits with failure and does not echo operator data."""
    home, _ = profile
    path = home / "policy.json"
    path.write_text('{"private-secret":"synthetic-secret"}')
    path.chmod(0o600)
    result = command(profile, "shared-doctor", "--policy", str(path), ok=False)
    assert result["status"] == "FAIL"
    assert "synthetic-secret" not in json.dumps(result)


@pytest.mark.skipif(os.name == "nt", reason="POSIX account lookup is not used on Windows")
def test_private_uid_without_account_record_remains_compatible(profile, monkeypatch):
    """Private containers without passwd records do not require protected enrollment."""
    home, _ = profile
    cfg = load_standalone_config(home)

    def unavailable():
        raise KeyError("Synthetic account has no passwd record")

    monkeypatch.setattr("qdrant_memory.shared_security.registry_path", unavailable)
    lease = WriterLease.for_config(home, cfg)
    lease.close()


@pytest.mark.skipif(os.name == "nt", reason="Protected mode requires POSIX permissions")
def test_protected_restart_recovers_prepared_plan(profile, monkeypatch):
    """Destination claims survive an interrupted shared plan and preserve idempotency."""
    from qdrant_memory.shared_service import SharedOwner, load_policy
    from qdrant_memory.standalone import open_runtime

    from ..standalone.test_shared_service import TOKENS

    home, _ = profile
    command(profile, "init")
    path, binding = protected_policy(home)
    make_private(home)
    args = {"text": "recover protected fact", "request_id": "recover-protected"}
    previous = os.umask(0o077)
    try:
        with open_runtime(home, shared_owner=True, destination_policy=binding) as runtime:
            owner = SharedOwner(runtime, load_policy(path))
            with monkeypatch.context() as patch:

                def interrupted(*args):
                    raise RuntimeError("Synthetic interruption before application")

                patch.setattr(owner, "_apply", interrupted)
                with pytest.raises(RuntimeError, match="Synthetic interruption"):
                    owner.call(TOKENS["hermes"], "qdrant_memory_add", args)
        with open_runtime(home, shared_owner=True, destination_policy=binding) as runtime:
            owner = SharedOwner(runtime, load_policy(path))
            first = owner.call(TOKENS["hermes"], "qdrant_memory_add", args)
            assert first["action"] == "ADD"
            assert owner.call(TOKENS["hermes"], "qdrant_memory_add", args) == first
            assert (
                len(
                    owner.call(TOKENS["hermes"], "qdrant_memory_search", {"query": "protected"})[
                        "memories"
                    ]
                )
                == 1
            )
    finally:
        os.umask(previous)


@pytest.mark.skipif(os.name == "nt", reason="Protected mode requires POSIX permissions")
def test_protected_collection_schema_is_checked_before_ledger(profile):
    """A known incompatible schema is not repaired or admitted for recovery."""
    from qdrant_memory.qdrant_store import IDENTITY_ID, CollectionCompatibilityError
    from qdrant_memory.standalone import open_runtime

    home, _ = profile
    command(profile, "init")
    with open_runtime(home) as runtime:
        runtime.store.client.set_payload(
            runtime.store.collection, {"_qdrant_memory_schema": 2}, points=[IDENTITY_ID], wait=True
        )
    _, binding = protected_policy(home)
    make_private(home)
    with pytest.raises(CollectionCompatibilityError):
        with open_runtime(home, shared_owner=True, destination_policy=binding):
            pytest.fail("Incompatible schema was admitted")
