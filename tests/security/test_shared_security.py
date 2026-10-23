"""Protected destinations reject unsafe admission before touching memory."""

import argparse
import errno
import json
import os
from contextlib import closing
from pathlib import Path

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
    cfg = load_standalone_config(home)
    with pytest.raises(ValueError):
        validate_destination(home, cfg, binding)
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
    cfg = load_standalone_config(home)
    with pytest.raises((PermissionError, ValueError)):
        validate_destination(home, cfg, binding)
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX registry admission requires POSIX permissions")
@pytest.mark.parametrize("failure", [errno.EACCES, errno.EPERM, errno.EROFS, errno.EIO])
@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_registry_creation_errors_preserve_only_safe_private_fallback(
    profile, monkeypatch, failure, protected, existing
):
    """Permission/read-only errors allow legacy admission only without a claim registry."""
    from qdrant_memory.shared_security import registry_path

    home, _ = profile
    _, binding = protected_policy(home)
    make_private(home)
    cfg = load_standalone_config(home)
    root = registry_path()
    if existing:
        root.mkdir(mode=0o700, parents=True)
    original = Path.mkdir

    def unavailable(path, *args, **kwargs):
        if path == root:
            raise OSError(failure, "Synthetic registry creation error")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", unavailable)
    if not protected and not existing and failure != errno.EIO:
        lease = WriterLease.for_config(home, cfg)
        lease.close()
    else:
        with pytest.raises(OSError) as caught:
            WriterLease.for_config(home, cfg, destination_policy=binding if protected else None)
        assert caught.value.errno == failure


@pytest.mark.skipif(os.name == "nt", reason="Protected mode requires POSIX permissions")
@pytest.mark.parametrize("stage", ["serialize", "sync", "publish"])
def test_interrupted_claim_is_not_published_and_can_be_retried(profile, monkeypatch, stage):
    """Readers never observe partial claims; a failed publication leaves no final file."""
    from qdrant_memory import shared_security

    home, _ = profile
    _, binding = protected_policy(home)
    make_private(home)
    cfg = load_standalone_config(home)

    def interrupted(*args, **kwargs):
        if stage == "serialize":
            args[1].write('{"partial":')
            args[1].flush()
        assert shared_security.read_claim(cfg) is None
        raise OSError(errno.EIO, "Synthetic interrupted claim")

    with monkeypatch.context() as patch:
        target, name = {
            "serialize": (shared_security.json, "dump"),
            "sync": (shared_security.os, "fsync"),
            "publish": (shared_security.os, "replace"),
        }[stage]
        patch.setattr(target, name, interrupted)
        with pytest.raises(OSError, match="Synthetic interrupted claim"):
            WriterLease.for_config(home, cfg, destination_policy=binding)
    assert shared_security.read_claim(cfg) is None
    assert not list(shared_security.registry_path().glob(".claim-*"))
    lease = WriterLease.for_config(home, cfg, destination_policy=binding)
    lease.close()
    assert shared_security.read_claim(cfg) == binding


@pytest.mark.parametrize("mode", ["shared-serve", "shared-doctor"])
@pytest.mark.parametrize(
    "spelling",
    [
        "absolute",
        "parent",
        pytest.param(
            "symlink",
            marks=pytest.mark.skipif(
                os.name == "nt", reason="Symlink creation requires Windows privileges"
            ),
        ),
    ],
)
def test_shared_cli_policy_cannot_escape_invocation_directory(
    profile, tmp_path, monkeypatch, capsys, mode, spelling
):
    """Shared CLI policy selection is checked before reading an outside file."""
    from qdrant_memory.standalone import main

    home, _ = profile
    outside = policy(tmp_path)
    argument = outside
    if spelling == "parent":
        argument = Path("..") / outside.name
    elif spelling == "symlink":
        argument = home / "policy-link.json"
        argument.symlink_to(outside)
    monkeypatch.chdir(home)
    assert main(["--home", str(home), mode, "--policy", str(argument)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["type"] == "OperatorPathOutsideRootError"


@pytest.mark.skipif(os.name == "nt", reason="Protected mode requires POSIX permissions")
def test_doctor_distinguishes_registered_idle_and_active_owner(profile):
    """A valid claim does not turn an occupied writer lock into a health proof."""
    from qdrant_memory.shared_security import doctor

    home, _ = profile
    command(profile, "init")
    path, binding = protected_policy(home)
    make_private(home)
    cfg = load_standalone_config(home)
    lease = WriterLease.for_config(home, cfg, destination_policy=binding)
    try:
        active = {c["code"]: c["status"] for c in doctor(home, path)["checks"]}
        assert active["local_ownership"] == "PASS"
        assert active["local_writer"] == "UNVERIFIED"
    finally:
        lease.close()
    idle = {c["code"]: c["status"] for c in doctor(home, path)["checks"]}
    assert idle["local_writer"] == "PASS"
    assert idle["credential_isolation"] == "UNVERIFIED"


@pytest.mark.skipif(os.name == "nt", reason="Protected mode requires POSIX permissions")
def test_local_writer_probe_reports_unverified_when_the_platform_refuses(profile, monkeypatch):
    """A lock probe that the OS rejects is inconclusive, never a failed check."""
    import portalocker

    from qdrant_memory.shared_security import doctor

    home, _ = profile
    command(profile, "init")
    path, binding = protected_policy(home)
    make_private(home)
    cfg = load_standalone_config(home)
    WriterLease.for_config(home, cfg, destination_policy=binding).close()

    def refuse(*_args, **_kwargs):
        raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(portalocker, "lock", refuse)
    statuses = {c["code"]: c["status"] for c in doctor(home, path)["checks"]}
    assert statuses["local_writer"] == "UNVERIFIED"
    assert statuses["owner_state"] == "PASS"


@pytest.mark.skipif(os.name == "nt", reason="Protected mode requires POSIX permissions")
def test_local_writer_probe_is_unverified_when_the_lock_file_cannot_be_opened(profile, monkeypatch):
    """A read-only mount cannot answer the probe; the check stays inconclusive."""
    from qdrant_memory.shared_security import doctor

    home, _ = profile
    command(profile, "init")
    path, binding = protected_policy(home)
    make_private(home)
    cfg = load_standalone_config(home)
    WriterLease.for_config(home, cfg, destination_policy=binding).close()

    original_open = Path.open

    def refuse(self, *args, **kwargs):
        if self.suffix == ".lock":
            raise OSError(errno.EROFS, "Read-only file system")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refuse)
    statuses = {c["code"]: c["status"] for c in doctor(home, path)["checks"]}
    assert statuses["local_writer"] == "UNVERIFIED"


@pytest.mark.parametrize("mismatch", [None, "dimensions", "fingerprint", "schema", "missing"])
def test_remote_doctor_checks_existing_schema_without_mutation(profile, monkeypatch, mismatch):
    """The remote read interface validates identity and never initializes or repairs it."""
    from qdrant_client import QdrantClient

    from qdrant_memory.qdrant_store import IDENTITY_ID
    from qdrant_memory.shared_security import doctor

    home, _ = profile
    command(profile, "init")
    cfg = load_standalone_config(home)
    storage = cfg["qdrant"]["path"]
    collection = cfg["qdrant"]["collection"]
    with closing(QdrantClient(path=storage)) as client:
        if mismatch == "missing":
            client.delete(collection, points_selector=[IDENTITY_ID], wait=True)
        elif mismatch in {"fingerprint", "schema"}:
            key = "embedding_fingerprint" if mismatch == "fingerprint" else "_qdrant_memory_schema"
            client.set_payload(collection, {key: "incompatible"}, points=[IDENTITY_ID], wait=True)
        before = client.count(collection, exact=True).count
    if mismatch == "dimensions":
        cfg["embedding"]["dimensions"] += 1
    cfg["qdrant"].update(mode="server", url="https://qdrant.invalid:6333")
    (home / "qdrant-memory.json").write_text(json.dumps(cfg))
    path = policy(home)
    make_private(home)
    # Exercise the real Qdrant read methods without requiring a network service.
    monkeypatch.setattr(
        "qdrant_memory.qdrant_store.build_client", lambda cfg: QdrantClient(path=storage)
    )
    report = {c["code"]: c["status"] for c in doctor(home, path)["checks"]}
    assert report["database_schema"] == ("PASS" if mismatch is None else "FAIL")
    assert report["credential_isolation"] == "UNVERIFIED"
    with closing(QdrantClient(path=storage)) as client:
        assert client.count(collection, exact=True).count == before


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
    startup = open_runtime(home, shared_owner=True, destination_policy=binding)
    with pytest.raises(CollectionCompatibilityError), startup:
        pass


@pytest.mark.skipif(os.name == "nt", reason="Path URI checks use POSIX spelling")
def test_relative_home_reaches_the_same_state_paths(profile):
    """A relative --home value is resolved before read-only state access."""
    import subprocess
    import sysconfig

    home, env = profile
    assert command(profile, "init")["ok"]
    result = subprocess.run(
        [
            str(
                Path(sysconfig.get_path("scripts"))
                / ("qdrant-memory.exe" if os.name == "nt" else "qdrant-memory")
            ),
            "--home",
            ".",
            "list",
        ],
        cwd=home,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["collection"] == "qdrant_memory"


@pytest.mark.skipif(os.name == "nt", reason="Protected mode requires POSIX permissions")
def test_retry_passes_the_protected_policy_to_the_writer_lease(profile, monkeypatch, capsys):
    """Retry hands its explicit policy to the lease that owns destination admission.

    This covers the CLI wiring. Real admission refusal and acceptance are covered
    by test_claim_blocks_other_profile_and_private_admission, which drives
    WriterLease and admit_claim directly on an enrolled destination.
    """
    from types import SimpleNamespace

    from qdrant_memory import cli
    from qdrant_memory.cli import main, register_cli

    home, _ = profile
    path, binding = protected_policy(home)
    make_private(home)
    seen = []

    def capture(home_argument, cfg, destination_policy=None):
        """Record the admission input, then reproduce the refusal path."""
        seen.append(destination_policy)
        raise PermissionError("Destination requires its protected shared owner policy")

    monkeypatch.setattr(cli, "WriterLease", SimpleNamespace(for_config=capture))
    parser = argparse.ArgumentParser()
    register_cli(parser)
    monkeypatch.setattr(cli, "active_home", lambda: home)
    # The protected policy must stay inside the operator's invocation directory.
    monkeypatch.chdir(home)
    assert main(parser.parse_args(["retry"])) == 1
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "PermissionError"
    assert seen == [None]
    assert main(parser.parse_args(["retry", "--policy", str(path)])) == 1
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "PermissionError"
    assert seen == [None, binding]


@pytest.mark.skipif(os.name == "nt", reason="Protected mode requires POSIX permissions")
def test_retry_rejects_a_policy_without_a_destination(profile, monkeypatch, capsys):
    """An explicit policy that cannot admit a destination is reported, not ignored."""
    from qdrant_memory.cli import main, register_cli

    home, _ = profile
    path = policy(home)
    make_private(home)
    parser = argparse.ArgumentParser()
    register_cli(parser)
    monkeypatch.setattr("qdrant_memory.cli.active_home", lambda: home)
    monkeypatch.chdir(home)
    assert main(parser.parse_args(["retry", "--policy", str(path)])) == 1
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "ValueError"
