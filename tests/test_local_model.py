"""Pinned model downloads become usable only after complete verification."""

import hashlib
import json
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
    client = httpx.Client
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.scheme == "https"
        assert local_model.MODEL_REVISION in str(request.url)
        assert "authorization" not in request.headers
        return httpx.Response(200, content=data[request.url.path.rsplit("/", 1)[-1]])

    monkeypatch.setattr(
        httpx, "Client", lambda **kw: client(**kw, transport=httpx.MockTransport(respond))
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
        with pytest.raises(portalocker.exceptions.LockException):
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
