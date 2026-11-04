"""Pinned model downloads become usable only after complete verification."""

import asyncio
import hashlib
import json
import os
import stat
from pathlib import Path

import httpx
import portalocker
import pytest


@pytest.fixture
def model_source(monkeypatch):
    """Serve synthetic pinned artifacts through the real streaming download path."""
    from qdrant_memory import local_model

    data = {name: ("synthetic " + name).encode() for name in local_model.ARTIFACTS}
    monkeypatch.setattr(
        local_model,
        "ARTIFACTS",
        {
            name: (hashlib.sha256(content).hexdigest(), len(content))
            for name, content in data.items()
        },
    )
    client = httpx.AsyncClient
    requests = []

    async def respond(request):
        requests.append(request)
        assert request.url.scheme == "https"
        assert local_model.MODEL_REVISION in str(request.url)
        assert "authorization" not in request.headers
        value = data[request.url.path.rsplit("/", 1)[-1]]
        if callable(value):
            return await value(request)
        if isinstance(value, httpx.AsyncByteStream):
            return httpx.Response(200, stream=value)
        return httpx.Response(200, content=value)

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: client(**kw, transport=httpx.MockTransport(respond))
    )
    return local_model, data, requests


def test_preparation_is_verified_idempotent_and_offline_on_repeat(tmp_path, model_source):
    """Publish only complete caches and avoid network on a successful repeat."""
    module, data, requests = model_source
    root = tmp_path / "models"
    target = module.prepare_model(root)
    assert target == module.verify_model(root)
    assert json.loads((target / "ready.json").read_text())["pipeline"] == module.pipeline_identity()
    assert len(requests) == len(data)
    assert module.prepare_model(root) == target
    assert len(requests) == len(data)


def test_corrupt_download_never_becomes_ready_and_can_be_retried(tmp_path, model_source):
    """A failed transfer remains unusable and a subsequent call can recover."""
    module, data, _ = model_source
    first = next(iter(data))
    original = data[first]
    data[first] = original[:-1] + bytes([original[-1] ^ 1])
    root = tmp_path / "models"
    with pytest.raises(module.LocalModelIntegrityError):
        module.prepare_model(root)
    with pytest.raises(module.LocalModelNotPreparedError):
        module.verify_model(root)
    assert not list(root.glob(".prepare-*"))
    data[first] = original
    assert module.prepare_model(root) == module.verify_model(root)


@pytest.mark.parametrize("transfer_seconds, succeeds", [(0.3, True), (3, False)])
def test_slow_download_has_a_fresh_bounded_window_per_artifact(
    tmp_path, model_source, monkeypatch, transfer_seconds, succeeds
):
    """Allow slow complete transfers without publishing an over-budget cache."""
    module, data, requests = model_source

    class Delayed(httpx.AsyncByteStream):
        def __init__(self, content):
            self.content = content

        async def __aiter__(self):
            await asyncio.sleep(transfer_seconds)
            yield self.content

    for name, content in list(data.items()):
        data[name] = Delayed(content)
    monkeypatch.setattr(module, "ARTIFACT_DOWNLOAD_SECONDS", 1)
    if succeeds:
        assert module.prepare_model(tmp_path) == module.verify_model(tmp_path)
        assert len(requests) == len(data)
    else:
        with pytest.raises(module.LocalModelIntegrityError, match="time limit"):
            module.prepare_model(tmp_path)
        with pytest.raises(module.LocalModelNotPreparedError):
            module.verify_model(tmp_path)
        assert not list(tmp_path.glob(".prepare-*"))


def test_slow_fragments_are_cancelled_before_a_megabyte_and_release_resources(
    tmp_path, model_source, monkeypatch
):
    """Continuous network activity must not evade the total deadline."""
    module, data, _ = model_source
    first = next(iter(data))
    original = data[first]

    class Drip(httpx.AsyncByteStream):
        chunks = 0
        cancelled = False
        closed = False

        async def __aiter__(self):
            try:
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    self.chunks += 1
                    yield b"x"
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        async def aclose(self):
            self.closed = True

    drip = Drip()
    data[first] = drip
    monkeypatch.setattr(module, "ARTIFACT_DOWNLOAD_SECONDS", 0.1)
    with pytest.raises(module.LocalModelIntegrityError, match="time limit"):
        module.prepare_model(tmp_path)
    assert drip.chunks < 100
    assert drip.cancelled and drip.closed
    assert not list(tmp_path.glob(".prepare-*"))
    with pytest.raises(module.LocalModelNotPreparedError):
        module.verify_model(tmp_path)
    # The next attempt must acquire the released lock and publish verified bytes.
    data[first] = original
    assert module.prepare_model(tmp_path) == module.verify_model(tmp_path)


def test_total_download_budget_cancels_a_sequence_of_individually_valid_transfers(
    tmp_path, model_source, monkeypatch
):
    """Per-artifact windows cannot multiply beyond the overall network budget."""
    module, data, requests = model_source
    original = dict(data)

    class Delayed(httpx.AsyncByteStream):
        def __init__(self, content):
            self.content = content

        async def __aiter__(self):
            await asyncio.sleep(0.1)
            yield self.content

    for name, content in original.items():
        data[name] = Delayed(content)
    monkeypatch.setattr(module, "ARTIFACT_DOWNLOAD_SECONDS", 1)
    monkeypatch.setattr(module, "TOTAL_DOWNLOAD_SECONDS", 0.25)
    with pytest.raises(module.LocalModelIntegrityError, match="total download time limit"):
        module.prepare_model(tmp_path)
    assert len(requests) < len(data)
    assert not list(tmp_path.glob(".prepare-*"))
    with pytest.raises(module.LocalModelNotPreparedError):
        module.verify_model(tmp_path)
    data.update(original)
    assert module.prepare_model(tmp_path) == module.verify_model(tmp_path)


def test_deadline_cancels_waiting_for_response_headers(tmp_path, model_source, monkeypatch):
    """The artifact budget starts before response headers arrive."""
    module, data, _ = model_source
    cancelled = False

    async def headers(_request):
        nonlocal cancelled
        try:
            await asyncio.sleep(1)
            return httpx.Response(200, content=b"late")
        except asyncio.CancelledError:
            cancelled = True
            raise

    data[next(iter(data))] = headers
    monkeypatch.setattr(module, "ARTIFACT_DOWNLOAD_SECONDS", 0.05)
    with pytest.raises(module.LocalModelIntegrityError, match="time limit"):
        module.prepare_model(tmp_path)
    assert cancelled
    assert not list(tmp_path.glob(".prepare-*"))


def test_https_redirect_guard_runs_before_a_downgraded_request(tmp_path, model_source):
    """Async HTTP hooks retain the pre-request HTTPS-only boundary."""
    module, data, requests = model_source

    async def redirect(_request):
        return httpx.Response(302, headers={"location": "http://example.invalid/model"})

    data[next(iter(data))] = redirect
    with pytest.raises(module.LocalModelIntegrityError, match="HTTPS"):
        module.prepare_model(tmp_path)
    assert len(requests) == 1
    assert not list(tmp_path.glob(".prepare-*"))


def test_synchronous_preparation_works_inside_an_existing_event_loop(tmp_path, model_source):
    """The caller's event loop must not prevent explicit synchronous setup."""
    module, _, _ = model_source

    async def caller():
        return module.prepare_model(tmp_path)

    assert asyncio.run(caller()) == module.verify_model(tmp_path)


@pytest.mark.parametrize("filename", ["model_optimized.onnx", "tokenizer.json", "ready.json"])
def test_published_cache_corruption_is_not_silently_replaced(tmp_path, model_source, filename):
    """Reject damaged completed caches without overwriting an installed model."""
    module, _, requests = model_source
    target = module.prepare_model(tmp_path)
    (target / filename).write_bytes(b"corrupt")
    count = len(requests)
    with pytest.raises(module.LocalModelIntegrityError):
        module.prepare_model(tmp_path)
    assert len(requests) == count
    assert (target / filename).read_bytes() == b"corrupt"


def test_parallel_preparation_respects_ownership(tmp_path, model_source):
    """A competing preparation cannot bypass the provisioning lock."""
    module, _, requests = model_source
    with portalocker.Lock(tmp_path / ".prepare.lock", mode="a", timeout=0):
        with pytest.raises(module.LocalModelPreparationBusyError, match="already in progress"):
            module.prepare_model(tmp_path)
    assert requests == []


@pytest.mark.parametrize("where", ["root", "ancestor", "target", "artifact", "marker", "lock"])
def test_cache_symlinks_cannot_escape_the_selected_root(tmp_path, model_source, where):
    """Refuse symlink substitutions throughout model cache preparation."""
    module, _, _ = model_source
    root = tmp_path / "cache"
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        if where == "root":
            root.symlink_to(outside, target_is_directory=True)
        elif where == "ancestor":
            root.symlink_to(outside, target_is_directory=True)
            root = root / "nested"
        else:
            target = module.prepare_model(root)
            if where == "target":
                target.rename(outside / "moved-model")
                target.symlink_to(outside / "moved-model", target_is_directory=True)
            elif where == "lock":
                (root / ".prepare.lock").unlink()
                (root / ".prepare.lock").symlink_to(outside / "lock")
            else:
                item = target / ("ready.json" if where == "marker" else "tokenizer.json")
                original = item.read_bytes()
                item.unlink()
                (outside / "artifact").write_bytes(original)
                item.symlink_to(outside / "artifact")
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account cannot create symlinks")
        raise
    with pytest.raises(module.LocalModelIntegrityError):
        module.prepare_model(root)


def test_manifest_contains_only_flat_nonexecutable_immutable_artifacts():
    """No archive extraction, executable download or moving model revision is allowed."""
    from qdrant_memory import local_model

    assert len(local_model.MODEL_REVISION) == 40
    assert local_model.DOWNLOAD_BYTES == sum(size for _, size in local_model.ARTIFACTS.values())
    for name, (digest, size) in local_model.ARTIFACTS.items():
        assert Path(name).name == name
        assert "/" not in name
        assert "\\" not in name
        assert name.endswith((".json", ".onnx"))
        assert len(digest) == 64
        assert size > 0


def test_model_files_are_synced_before_cache_publication(tmp_path, model_source, monkeypatch):
    """Publish only after artifact bytes and the completion marker are flushed."""
    module, data, _ = model_source
    synced = []
    sync = os.fsync
    rename = Path.rename

    def record_sync(descriptor):
        """Record real file/directory synchronization in publication order."""
        synced.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        return sync(descriptor)

    def publish(path, target):
        """Check durability at the actual directory publication boundary."""
        assert synced.count("file") == len(data) + 1
        if module.sys.platform != "win32":
            assert synced[-1] == "directory"
        return rename(path, target)

    monkeypatch.setattr(module.os, "fsync", record_sync)
    monkeypatch.setattr(Path, "rename", publish)
    assert module.prepare_model(tmp_path) == module.verify_model(tmp_path)
    if module.sys.platform != "win32":
        assert synced.count("directory") == 2


def test_sync_failure_leaves_no_ready_cache_and_can_retry(tmp_path, model_source, monkeypatch):
    """A failed durability operation must not publish an apparently ready model."""
    module, _, _ = model_source
    sync = os.fsync

    def fail(descriptor):
        raise OSError("synthetic sync failure")

    monkeypatch.setattr(module.os, "fsync", fail)
    with pytest.raises(OSError, match="synthetic sync failure"):
        module.prepare_model(tmp_path)
    with pytest.raises(module.LocalModelNotPreparedError):
        module.verify_model(tmp_path)
    assert not list(tmp_path.glob(".prepare-*"))
    monkeypatch.setattr(module.os, "fsync", sync)
    assert module.prepare_model(tmp_path) == module.verify_model(tmp_path)


def test_nonempty_destination_created_at_publication_is_preserved(
    tmp_path, model_source, monkeypatch
):
    """A noncooperating writer's nonempty destination must never be overwritten."""
    module, _, _ = model_source
    rename = Path.rename
    destinations = []

    def race(path, target):
        """Introduce another directory immediately before the filesystem rename."""
        target.mkdir()
        (target / "unrelated-data.txt").write_text("preserve this data")
        destinations.append(target)
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", race)
    with pytest.raises(OSError):
        module.prepare_model(tmp_path)
    assert (destinations[0] / "unrelated-data.txt").read_text() == "preserve this data"
    assert not (destinations[0] / "ready.json").exists()
    assert not list(tmp_path.glob(".prepare-*"))
