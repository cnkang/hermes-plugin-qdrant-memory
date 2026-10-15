"""Explicit new-profile setup over the existing initialization and lease boundary."""

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

from .config import DEFAULTS, merge, validate
from .private_paths import checked_absolute_path


class ManagedProfileConflictError(ValueError):
    """Refuse an existing, changed or unowned destination without overwriting it."""


def _read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ManagedProfileConflictError("Unsafe profile configuration or setup marker")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ManagedProfileConflictError("Invalid profile configuration or setup marker") from exc


def _write_new(path, value, *, replace=False):
    """Publish a private configuration atomically under the provisioning lock."""
    descriptor, temporary = tempfile.mkstemp(prefix=".setup-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        if not replace and (path.exists() or path.is_symlink()):
            raise ManagedProfileConflictError("Profile destination changed during setup")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _cpu_settings(args):
    """Accept only settings belonging to the explicitly selected CPU pipeline."""
    from .local_model import DIMENSIONS, MODEL_NAME

    if args.base_url is not None:
        raise ValueError("CPU embedding does not use a service endpoint")
    settings = {
        "model": MODEL_NAME if args.model is None else args.model,
        "dimensions": DIMENSIONS if args.dimensions is None else args.dimensions,
    }
    if args.model_cache_dir is not None:
        settings["model_cache_dir"] = str(args.model_cache_dir)
    return settings


def _service_settings(args):
    """Keep existing Ollama defaults and require explicit remote service settings."""
    if args.embedding_provider == "ollama":
        return {
            "model": "qwen3-embedding:4b" if args.model is None else args.model,
            "dimensions": 2560 if args.dimensions is None else args.dimensions,
            "base_url": args.base_url or "http://127.0.0.1:11434",
        }
    if not args.model or args.dimensions is None or not args.base_url:
        raise ValueError("Remote embedding requires model, dimensions and a service endpoint")
    return {"model": args.model, "dimensions": args.dimensions, "base_url": args.base_url}


def configuration(args):
    """Build non-secret settings only for the explicitly selected provider."""
    provider = args.embedding_provider
    emb = {"mode": "plugin", "provider": provider, "distance": "Cosine"}
    emb.update(_cpu_settings(args) if provider == "fastembed" else _service_settings(args))
    if not emb["model"]:
        raise ValueError("An embedding model is required")
    if provider != "fastembed":
        if args.model_cache_dir is not None:
            raise ValueError("A model cache applies only to explicitly selected CPU embedding")
        emb["api_key_env"] = args.embedding_api_key_env
    cfg = {
        "schema_version": 1,
        "scope": {"user_id": args.user_id, "agent_id": args.agent_id},
        "qdrant": {"mode": "embedded", "collection": "qdrant_memory"},
        "embedding": emb,
    }
    validate(merge(DEFAULTS, cfg))
    return cfg


def _claim_profile(home, cfg, owner):
    """Create or validate ownership while the caller holds the provisioning lock."""
    if home.is_symlink():
        raise ManagedProfileConflictError("Unsafe profile destination")
    home.mkdir(mode=0o700, exist_ok=True)
    marker = home / ".setup-owner.json"
    path = home / "qdrant-memory.json"
    if marker.exists() or marker.is_symlink():
        if _read(marker) != owner:
            if path.exists() or path.is_symlink():
                raise ManagedProfileConflictError("Setup cannot change an owned profile's settings")
            # A failed first attempt leaves only the marker; a corrected request may retry.
            _write_new(marker, owner, replace=True)
        if (path.exists() or path.is_symlink()) and _read(path) != cfg:
            raise ManagedProfileConflictError("Profile settings have changed")
    elif any(home.iterdir()):
        raise ManagedProfileConflictError("Setup cannot adopt an existing profile")
    else:
        _write_new(marker, owner)
    os.chmod(home, 0o700)
    return path


def setup_profile(args):
    """Create/retry an owned private embedded profile, preserving all existing data.

    Model/service selection is required. The same request may be retried after
    failure; a different request or an unrelated profile is refused. Initialization
    uses Runtime's existing CLI implementation, including WriterLease and recovery.
    """
    import portalocker

    from . import cli
    from .standalone import load_standalone_config

    cfg = configuration(args)
    if cfg["embedding"]["provider"] == "fastembed":
        from .local_embedding import require_local_dependencies

        require_local_dependencies()
    try:
        home = checked_absolute_path(args.profile)
    except ValueError as exc:
        raise ManagedProfileConflictError(
            "Choose a new canonical private profile directory"
        ) from exc
    home.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = home.parent / f".{home.name}.setup.lock"
    if lock.is_symlink():
        raise ManagedProfileConflictError("Unsafe profile setup lock")
    digest = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
    owner = {"owner": "qdrant-memory-setup", "schema": 1, "configuration_sha256": digest}
    if cfg["embedding"]["provider"] == "fastembed":
        from .local_model import prepare_model

        # Prepare the shared model cache before taking the profile lock so a slow
        # download does not block retries or a concurrent setup.
        prepare_model(cfg["embedding"].get("model_cache_dir"))
    lock_handle = portalocker.Lock(lock, mode="a", timeout=0)
    try:
        lock_handle.acquire()
    except portalocker.exceptions.LockException as exc:
        raise ManagedProfileConflictError("Profile setup is already running") from exc
    try:
        path = _claim_profile(home, cfg, owner)
        if not path.exists():
            _write_new(path, cfg)
        command = argparse.Namespace(qdrant_command="init", collection=None, existing="use")
        result = cli.run(
            command, home, config_loader=load_standalone_config, secret_reader=os.environ.get
        )
        if result.get("ok", True) is False:
            raise RuntimeError("Profile initialization did not complete")
    finally:
        lock_handle.release()
    return {
        "ok": True,
        "ready": True,
        "profile": str(home),
        "embedding_provider": cfg["embedding"]["provider"],
        "model": cfg["embedding"]["model"],
        "dimensions": cfg["embedding"]["dimensions"],
    }
