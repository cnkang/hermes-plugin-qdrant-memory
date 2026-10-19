"""Trusted local destination binding and non-destructive deployment diagnostics."""

import json
import os
import sqlite3
import stat
import tempfile
from contextlib import closing
from pathlib import Path

from .config import backend_destination, embedding_config, fingerprint, ledger_namespace
from .models import digest
from .private_paths import checked_absolute_path

MODES = {
    "local-embedded": "embedded",
    "protected-server": "server",
    "protected-cloud": "cloud",
    "external-unverified": None,
}


def private_entry(path, *, directory=False):
    """Check ownership and permissions without repairing an unsafe destination."""
    path = checked_absolute_path(Path(path).absolute())
    info = path.lstat()
    if os.name == "nt":
        raise PermissionError("Protected mode requires verified POSIX ownership and permissions")
    for parent in path.parents:
        parent_info = parent.stat()
        if parent_info.st_mode & 0o022 and not parent_info.st_mode & stat.S_ISVTX:
            raise PermissionError("Protected state has a writable ancestor directory")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or (not directory and info.st_nlink != 1)
    ):
        raise PermissionError("Protected owner state must be private and owned by this account")
    return path


def check_owner_state(home, cfg):
    """Inspect state, sidecars and embedded files before any recovery can write."""
    home = private_entry(home, directory=True)
    private_entry(home / "qdrant-memory.json")
    # Configuration loading canonicalizes paths; inspect the operator's original
    # embedded spelling as well so that normalization cannot conceal a symlink.
    raw = json.loads((home / "qdrant-memory.json").read_text(encoding="utf-8"))
    if cfg["qdrant"]["mode"] == "embedded":
        storage = Path(raw.get("qdrant", {}).get("path") or "qdrant-memory/qdrant").expanduser()
        checked_absolute_path(storage if storage.is_absolute() else home / storage)
    roots = [home / "qdrant-memory"]
    if cfg["qdrant"]["mode"] == "embedded":
        roots.append(Path(cfg["qdrant"]["path"]))
    for root in roots:
        _check_private_tree(root)


def _check_private_tree(root):
    """Reject exposed or substituted state entries without repairing their modes."""
    checked_absolute_path(root)
    if not root.exists():
        return
    private_entry(root, directory=True)
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            private_entry(Path(parent) / name, directory=True)
        for name in files:
            private_entry(Path(parent) / name)


def validate_destination(home, cfg, binding):
    """Match explicit trusted policy to existing namespace and schema semantics."""
    expected = {
        "backend": cfg["qdrant"]["mode"],
        "destination": list(backend_destination(cfg["qdrant"])),
        "collection": cfg["qdrant"]["collection"],
        "embedding_fingerprint": fingerprint(embedding_config(cfg)),
        "owner_profile": digest(str(Path(home).expanduser().resolve())),
        "schema_version": 1,
    }
    if (
        not isinstance(binding, dict)
        or set(binding) != set(expected) | {"security_mode"}
        or any(binding[key] != value for key, value in expected.items())
        or type(binding["schema_version"]) is not int
        or not isinstance(binding["security_mode"], str)
        or binding["security_mode"] not in MODES
    ):
        raise ValueError("Protected destination policy does not match this profile")
    mode = binding["security_mode"]
    if mode != "external-unverified" and MODES[mode] != expected["backend"]:
        raise ValueError("Security mode does not match the backend")
    if expected["backend"] == "embedded" and mode != "local-embedded":
        raise ValueError("Embedded protection requires local-embedded mode")
    check_owner_state(home, cfg)
    q = cfg["qdrant"]
    if mode in {"protected-server", "protected-cloud"} and (
        not q.get("api_key") or backend_destination(q)[1] != "https"
    ):
        raise ValueError("Protected remote mode requires TLS and an owner-only credential")


def registry_path():
    """Use a fixed account-local registry; untrusted clients cannot choose it."""
    if os.name == "nt":
        return Path.home() / ".qdrant-memory-owners"
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".qdrant-memory-owners"


def destination_key(cfg):
    """Reuse the canonical ledger namespace without exposing destination details."""
    return digest(ledger_namespace(cfg))


def claim_path(cfg):
    """Address a local claim using only an opaque destination digest."""
    return registry_path() / (destination_key(cfg) + ".json")


def read_claim(cfg):
    """Never trust an exposed or substituted registry entry."""
    root = registry_path()
    checked_absolute_path(root)
    if not root.exists():
        return None
    private_entry(root, directory=True)
    path = claim_path(cfg)
    if not path.exists() and not path.is_symlink():
        return None
    private_entry(path)
    if path.stat().st_size > 16384:
        raise ValueError("Invalid protected destination claim")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Invalid protected destination claim")
    return value


def admit_claim(home, cfg, binding):
    """Admit only the bound owner under the caller's account-wide destination lock."""
    existing = read_claim(cfg)
    if binding is None:
        if existing is not None:
            raise PermissionError("Destination requires its protected shared owner policy")
        return
    validate_destination(home, cfg, binding)
    if existing is not None:
        if existing != binding:
            raise PermissionError("Destination is already assigned to another protected owner")
        return
    path = claim_path(cfg)
    descriptor, temporary = tempfile.mkstemp(prefix=".claim-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(binding, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _recovery(home, cfg):
    """Read a quiescent ledger without opening or repairing its WAL files."""
    path = Path(home) / "qdrant-memory" / "state.db"
    if not path.exists():
        return "UNVERIFIED"
    if Path(str(path) + "-wal").exists():
        return "UNVERIFIED"
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, condition in (
            ("operations", "status IN ('PENDING','FAILED')"),
            ("events", "status IN ('PENDING','FAILED')"),
            ("shared_requests_v1", "done=0"),
            ("qdrant_reset_intents", "1=1"),
            ("qdrant_scope_deletes", "1=1"),
        ):
            if (
                table in tables
                and db.execute(
                    f"SELECT 1 FROM {table} WHERE collection=? AND {condition} LIMIT 1",
                    (ledger_namespace(cfg),),
                ).fetchone()
            ):
                return "FAIL"
    return "PASS"


def _database(cfg):
    """Use remote read APIs only; embedded inspection must not open local Qdrant."""
    if cfg["qdrant"]["mode"] == "embedded":
        return "UNVERIFIED"
    from .qdrant_store import IDENTITY_ID, build_client

    client = build_client(cfg)
    try:
        collection = cfg["qdrant"]["collection"]
        info = client.get_collection(collection)
        vectors = info.config.params.vectors
        emb = embedding_config(cfg)
        identity = client.retrieve(collection, ids=[IDENTITY_ID], with_payload=True)
        if (
            not isinstance(vectors, dict)
            or "dense" not in vectors
            or vectors["dense"].size != emb["dimensions"]
            or vectors["dense"].distance != emb["distance"]
            or not identity
            or identity[0].payload.get("_qdrant_memory_schema") != 1
            or identity[0].payload.get("embedding_fingerprint") != fingerprint(emb)
        ):
            return "FAIL"
        return "PASS"
    finally:
        client.close()


def doctor(home, policy_path):
    """Return stable redacted checks; never recover, write or create collections."""
    from .shared_service import load_policy_document
    from .standalone import load_standalone_config

    checks = []

    def add(code, status, message):
        checks.append({"code": code, "status": status, "message": message})

    def check(code, action, message):
        try:
            result = action()
            status = result if isinstance(result, str) else "PASS"
        except Exception:
            status = "FAIL"
        add(code, status, message)
        return status

    try:
        policy = load_policy_document(policy_path)
        cfg = load_standalone_config(home)
    except Exception:
        add(
            "configuration",
            "FAIL",
            "Check private policy permissions, grants and profile settings.",
        )
        return {"status": "FAIL", "checks": checks}
    home = Path(home).expanduser().absolute()
    add("policy", "PASS", "Private policy and fixed client grants validated.")
    binding = policy.get("destination")
    if binding is None:
        add("destination", "UNVERIFIED", "No protected destination binding is configured.")
    else:
        check(
            "destination",
            lambda: validate_destination(home, cfg, binding),
            "Trusted destination binding must match the profile.",
        )
    state = check(
        "owner_state",
        lambda: check_owner_state(home, cfg),
        "Owner profile, ledger and embedded files must be private.",
    )
    if state == "PASS":
        check(
            "recovery",
            lambda: _recovery(home, cfg),
            "Pending work requires owner recovery; an active WAL is not a readiness proof.",
        )
        check(
            "local_ownership",
            lambda: _check_claim(cfg, binding),
            "Existing local destination claim must match the owner policy.",
        )
        check(
            "local_writer",
            lambda: _local_writer(cfg),
            "An active local writer requires coordination; offline diagnostics cannot attest service health.",
        )
    else:
        add("recovery", "UNVERIFIED", "Unsafe owner state was not opened.")
    check(
        "database_schema",
        lambda: _database(cfg),
        "Remote collection schema and fingerprint must match; embedded inspection is deferred to startup.",
    )
    add(
        "loopback_design",
        "PASS",
        "The supported service binds literal IPv4 loopback in code; this check does not inspect a running listener.",
    )
    add(
        "service_readiness",
        "UNVERIFIED",
        "Offline checks do not establish running service readiness or lock availability.",
    )
    add(
        "credential_isolation",
        "UNVERIFIED",
        "Effective Qdrant key scope and exclusive credential distribution require deployment evidence.",
    )
    add(
        "backup",
        "UNVERIFIED",
        "Verify a quiescent coherent Owner and Qdrant backup and a tested restore procedure.",
    )
    return {"status": _diagnostic_status(checks), "checks": checks}


def _diagnostic_status(checks):
    """Give failed checks precedence over unverifiable deployment controls."""
    if any(c["status"] == "FAIL" for c in checks):
        return "FAIL"
    if any(c["status"] == "UNVERIFIED" for c in checks):
        return "UNVERIFIED"
    return "PASS"


def _local_writer(cfg):
    """Probe only an existing lock file; never create a diagnostic lock."""
    import portalocker

    path = registry_path() / (destination_key(cfg) + ".lock")
    if not path.exists():
        return "UNVERIFIED"
    private_entry(path)
    try:
        stream = path.open("r+b")
    except OSError:
        # A read-only mount cannot answer the probe; do not report a failure.
        return "UNVERIFIED"
    with stream:
        try:
            portalocker.lock(stream, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except (portalocker.LockException, OSError):
            # An occupied lock or a platform that refuses the probe is not a defect.
            return "UNVERIFIED"
    return "PASS"


def _check_claim(cfg, binding):
    existing = read_claim(cfg)
    if existing is None:
        return "UNVERIFIED"
    if existing != binding:
        raise PermissionError("Conflicting local owner claim")
    return "PASS"
