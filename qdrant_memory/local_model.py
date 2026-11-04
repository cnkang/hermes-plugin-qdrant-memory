"""Explicit preparation of a pinned, non-executable local embedding model.

Normal embedding startup only verifies this cache; it never downloads files.
Model bytes are versioned independently of profiles and memory storage.
"""

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .private_paths import checked_absolute_path

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_REPOSITORY = "Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
MODEL_REVISION = "faf4aa4225822f3bc6376869cb1164e8e3feedd0"
FASTEMBED_VERSION = "0.9.1"
DIMENSIONS = 384
MAX_TOKENS = 128
# SHA-256 and exact byte length at MODEL_REVISION. No archives or remote code.
ARTIFACTS = {
    "model_optimized.onnx": (
        "634d0f66c29dc934c8fa72b8a4fe91dd4d420a22f1d82a241058d4316e659a99",
        235052644,
    ),
    "tokenizer.json": (
        "fa685fc160bbdbab64058d4fc91b60e62d207e8dc60b9af5c002c5ab946ded00",
        17083009,
    ),
    "config.json": ("c8ec081fdad2df991bf5abbf18418fec7a5cdaa421f60ffb060a30040b8c376f", 673),
    "special_tokens_map.json": (
        "8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835",
        964,
    ),
    "tokenizer_config.json": (
        "0666eebf692422757e1dddf3c9fb1ded73ba3dc726c5828671fc89e45bf3609f",
        1416,
    ),
}
DOWNLOAD_BYTES = sum(size for _, size in ARTIFACTS.values())
ARTIFACT_DOWNLOAD_SECONDS = 15 * 60


class LocalModelNotPreparedError(RuntimeError):
    """Require explicit model preparation before starting a memory session."""


class LocalModelPreparationBusyError(RuntimeError):
    """Another process holds the model preparation lock; wait for it and retry."""


class LocalModelIntegrityError(ValueError):
    """Reject damaged files or unsafe cache paths without replacing user data."""


def default_cache_dir():
    """Choose a model-only cache independent of Hermes and memory profiles."""
    if sys.platform == "win32":
        return (
            Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
            / "QdrantMemory/models"
        )
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches/qdrant-memory/models"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "qdrant-memory/models"


def pipeline_identity():
    """Include immutable artifacts, inference engine and token/normalization policy."""
    files = json.dumps(ARTIFACTS, sort_keys=True)
    digest = hashlib.sha256(files.encode()).hexdigest()
    return f"fastembed-{FASTEMBED_VERSION}:{MODEL_REVISION}:{digest}:mean-l2:reject-{MAX_TOKENS}-v1"


def validate_local_config(emb):
    """Validate an explicitly selected CPU pipeline without importing its extras."""
    if emb.get("model") != MODEL_NAME or emb.get("dimensions") != DIMENSIONS:
        raise ValueError("Local embedding requires the supported model and 384 dimensions")
    if emb.get("model_revision", MODEL_REVISION) != MODEL_REVISION:
        raise ValueError("Unsupported local model revision")
    cache = emb.get("model_cache_dir")
    if cache is not None and (
        not isinstance(cache, str) or not Path(cache).expanduser().is_absolute()
    ):
        raise ValueError("Model cache must be an absolute path")
    if emb.get("api_key"):
        raise ValueError("Local embedding does not accept an API key")


def _root(cache_dir):
    """Resolve the operator-selected root, rejecting a substituted leaf symlink."""
    root = Path(cache_dir if cache_dir is not None else default_cache_dir()).expanduser()
    try:
        return checked_absolute_path(root)
    except ValueError as exc:
        raise LocalModelIntegrityError("Unsafe model cache root") from exc


def _target(root):
    return root / f"multilingual-minilm-{MODEL_REVISION}"


def _check_artifact(path, expected):
    """Hash a regular file before handing it to any parser or inference engine."""
    digest, size = expected
    if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
        raise LocalModelIntegrityError("Model artifact is missing, unsafe or has the wrong size")
    actual = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            actual.update(block)
    if actual.hexdigest() != digest:
        raise LocalModelIntegrityError("Model artifact hash mismatch")


def verify_model(cache_dir=None):
    """Verify a complete immutable cache without network or filesystem mutation."""
    root = _root(cache_dir)
    target = _target(root)
    if target.is_symlink():
        raise LocalModelIntegrityError("Unsafe model directory")
    if not target.exists():
        raise LocalModelNotPreparedError("Run prepare-model before starting local embedding")
    marker = target / "ready.json"
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
        raise LocalModelIntegrityError("Invalid model completion marker")
    try:
        completed = json.loads(marker.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise LocalModelIntegrityError("Invalid model completion marker") from exc
    if completed != {"pipeline": pipeline_identity()}:
        raise LocalModelIntegrityError("Incompatible model completion marker")
    for name, expected in ARTIFACTS.items():
        _check_artifact(target / name, expected)
    return target


async def _require_https(request):
    """Reject downgrade redirects before sending the redirected request."""
    if request.url.scheme != "https":
        raise LocalModelIntegrityError("Model downloads require HTTPS")


async def _download(client, path, name, expected):
    """Cancel the entire network transfer, including headers and tiny fragments."""
    url = f"https://huggingface.co/{MODEL_REPOSITORY}/resolve/{MODEL_REVISION}/{name}"
    received = 0
    try:
        async with asyncio.timeout(ARTIFACT_DOWNLOAD_SECONDS):
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                with path.open("xb") as stream:
                    async for block in response.aiter_bytes(1024 * 1024):
                        received += len(block)
                        if received > expected[1]:
                            raise LocalModelIntegrityError("Model download exceeded its size limit")
                        stream.write(block)
                    stream.flush()
                    os.fsync(stream.fileno())
    except TimeoutError as exc:
        raise LocalModelIntegrityError("Model download exceeded its time limit") from exc
    _check_artifact(path, expected)


async def _download_artifacts(staging):
    """Share one client while giving each artifact a separate cancellable window."""
    import httpx

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30, connect=10),
        follow_redirects=True,
        event_hooks={"request": [_require_https]},
    ) as client:
        for name, expected in ARTIFACTS.items():
            await _download(client, staging / name, name, expected)


def _prepare_downloads(staging):
    """Keep the synchronous preparation API usable from a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_download_artifacts(staging))
    else:
        # Wait for cancellation and stream/client cleanup before touching staging.
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(asyncio.run, _download_artifacts(staging)).result()


def _sync_directory(path):
    """Persist directory metadata where Python exposes directory synchronization."""
    if sys.platform == "win32":
        # Python cannot fsync a Windows directory handle; regular files are synced.
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def prepare_model(cache_dir=None):
    """Explicitly download/verify files, then publish one complete model atomically.

    A failed preparation removes only its own temporary directory. Existing
    completed models are verified, never silently overwritten or repaired.
    Rerunning after failure retries preparation; rerunning after success is offline.
    """
    import portalocker

    root = _root(cache_dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = root / ".prepare.lock"
    if lock_path.is_symlink():
        raise LocalModelIntegrityError("Unsafe model preparation lock")
    lock = portalocker.Lock(lock_path, mode="a", timeout=0)
    try:
        lock.acquire()
    except portalocker.exceptions.LockException as exc:
        raise LocalModelPreparationBusyError("Model preparation is already in progress") from exc
    try:
        target = _target(root)
        if target.exists() or target.is_symlink():
            return verify_model(root)
        staging = Path(tempfile.mkdtemp(prefix=".prepare-", dir=root))
        try:
            _prepare_downloads(staging)
            with (staging / "ready.json").open("x", encoding="utf-8") as marker:
                json.dump({"pipeline": pipeline_identity()}, marker)
                marker.flush()
                os.fsync(marker.fileno())
            _sync_directory(staging)
            if target.exists() or target.is_symlink():
                raise LocalModelIntegrityError("Model destination changed during preparation")
            staging.rename(target)
            _sync_directory(root)
        finally:
            # Never remove a completed model, profile, collection or ledger.
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
    finally:
        lock.release()
    return verify_model(root)
