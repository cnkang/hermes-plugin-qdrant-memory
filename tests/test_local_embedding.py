"""Optional CPU model preparation must not change existing embedding choices."""

import hashlib
import json
from copy import deepcopy

import pytest

from qdrant_memory.config import DEFAULTS, fingerprint, merge, validate
from qdrant_memory.embedding import HTTPEmbeddingProvider, build_embedder
from qdrant_memory.standalone import load_standalone_config

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def local_cfg(cache):
    """Select CPU embedding explicitly without changing shared defaults."""
    return merge(
        DEFAULTS,
        {
            "embedding": {
                "provider": "fastembed",
                "model": MODEL,
                "dimensions": 384,
                "model_cache_dir": str(cache),
            }
        },
    )


def test_cpu_provider_is_explicit_and_existing_defaults_are_unchanged(tmp_path):
    """Keep existing Ollama defaults intact when validating a CPU profile."""
    original = deepcopy(DEFAULTS)
    cfg = local_cfg(tmp_path / "models")
    validate(cfg)
    assert DEFAULTS == original
    assert DEFAULTS["embedding"]["provider"] == "ollama"


@pytest.mark.parametrize("provider", ["ollama", "openai-compatible"])
def test_existing_provider_fingerprints_keep_the_original_algorithm(provider):
    """Match the exact pre-CPU fingerprint serialization for existing providers."""
    emb = merge(DEFAULTS["embedding"], {"provider": provider})
    keys = (
        "provider",
        "model",
        "dimensions",
        "distance",
        "document_instruction",
        "query_instruction",
        "normalization_version",
        "base_url",
        "send_dimensions",
    )
    values = {key: emb.get(key) for key in keys}
    values["base_url"] = values["base_url"].rstrip("/")
    expected = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    assert fingerprint(emb) == expected


def test_cpu_identity_ignores_cache_location_but_includes_artifact_revision(tmp_path):
    """Separate model identity from machine-specific cache placement."""
    from qdrant_memory import local_model

    emb = local_cfg(tmp_path / "one")["embedding"]
    assert fingerprint(emb) == fingerprint({**emb, "model_cache_dir": str(tmp_path / "two")})
    assert local_model.MODEL_REVISION in local_model.pipeline_identity()
    assert fingerprint(emb) != fingerprint({**emb, "query_instruction": "query: "})


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "untrusted/arbitrary-model"},
        {"dimensions": 2560},
        {"model_revision": "main"},
        {"model_cache_dir": "relative/model/cache"},
        {"api_key": "not-a-local-provider-setting"},
    ],
)
def test_invalid_cpu_configuration_fails_before_storage(tmp_path, changes):
    """Reject unsupported models and settings without creating directories."""
    cfg = local_cfg(tmp_path / "models")
    cfg["embedding"].update(changes)
    with pytest.raises(ValueError):
        validate(cfg)
    assert not (tmp_path / "models").exists()


def test_cpu_profile_does_not_resolve_embedding_service_credentials(tmp_path):
    """Leave unrelated embedding service keys unread for local CPU inference."""
    cfg = local_cfg(tmp_path / "models")
    (tmp_path / "qdrant-memory.json").write_text(json.dumps(cfg))
    read = []

    def reader(key):
        read.append(key)
        return "not-a-url" if key == "EMBEDDING_API_KEY" else None

    loaded = load_standalone_config(tmp_path, secret_reader=reader)
    assert loaded["embedding"]["provider"] == "fastembed"
    assert "EMBEDDING_API_KEY" not in read


def test_cpu_startup_never_downloads_a_missing_model(tmp_path, monkeypatch):
    """Keep normal MCP startup free of implicit model downloads."""
    import httpx

    from qdrant_memory import local_model

    def forbidden(*args, **kwargs):
        pytest.fail("Normal provider startup must not download models")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    with pytest.raises(local_model.LocalModelNotPreparedError):
        build_embedder(None, local_cfg(tmp_path / "models"))
    assert not (tmp_path / "models").exists()


@pytest.mark.parametrize("provider", ["ollama", "openai-compatible"])
def test_http_provider_selection_does_not_prepare_or_import_cpu_models(provider, monkeypatch):
    """Existing HTTP choices do not acquire local model dependencies or files."""
    from qdrant_memory import local_model

    def forbidden(*args, **kwargs):
        pytest.fail("Existing providers must never prepare CPU models")

    monkeypatch.setattr(local_model, "prepare_model", forbidden)
    with_provider = merge(DEFAULTS, {"embedding": {"provider": provider}})
    embedder = build_embedder(None, with_provider, secret_reader=lambda key: None)
    assert isinstance(embedder, HTTPEmbeddingProvider)
    embedder.close()
