"""Exercise the optional inference boundary without installing a model backend."""

import sys
from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace

import pytest

from qdrant_memory import local_embedding as module
from qdrant_memory.config import DEFAULTS, merge
from qdrant_memory.local_model import FASTEMBED_VERSION, MODEL_NAME


@pytest.fixture
def backend(tmp_path, monkeypatch):
    """Capture tokenizer and inference calls at the optional dependency boundary."""
    events = []
    state = SimpleNamespace(vectors=None, texts=[], options=[])

    class Tokenizer:
        """Count synthetic whitespace tokens without applying truncation."""

        @classmethod
        def from_file(cls, path):
            """Record the verified tokenizer location."""
            events.append(("tokenizer", path))
            return cls()

        def no_truncation(self):
            """Require explicit input rejection instead of backend truncation."""
            events.append("no_truncation")

        def no_padding(self):
            """Keep token counts independent of backend padding."""
            events.append("no_padding")

        def encode(self, text):
            """Expose token IDs for the complete prefixed input."""
            return SimpleNamespace(ids=text.split())

    class Model:
        """Return deterministic vectors and capture inference settings."""

        def __init__(self, **kwargs):
            """Record CPU-only, offline construction parameters."""
            state.constructor = kwargs

        def embed(self, texts, **kwargs):
            """Preserve input order and optionally simulate malformed output."""
            state.texts.append(texts)
            state.options.append(kwargs)
            vectors = state.vectors
            if vectors is None:
                vectors = [[1.0] + [0.0] * 383 for _ in texts]
            return [SimpleNamespace(tolist=lambda vector=vector: vector) for vector in vectors]

    def verified(cache):
        """Require cache validation before optional inference construction."""
        events.append(("verified", cache))
        return tmp_path

    monkeypatch.setattr(module, "verify_model", verified)
    monkeypatch.setattr(module, "require_local_dependencies", lambda: (Model, Tokenizer))
    cfg = merge(
        DEFAULTS["embedding"],
        {
            "provider": "fastembed",
            "model": MODEL_NAME,
            "dimensions": 384,
            "model_cache_dir": str(tmp_path),
            "query_instruction": "query: ",
            "document_instruction": "document: ",
        },
    )
    return module.LocalEmbeddingProvider(cfg), state, events, tmp_path


def test_offline_cpu_backend_receives_prefixes_and_bounded_inference_settings(backend):
    """Keep the validated model path, input order and instruction prefixes intact."""
    provider, state, events, cache = backend
    assert events[0] == ("verified", str(cache))
    assert events[1] == ("tokenizer", str(cache / "tokenizer.json"))
    assert events[2:] == ["no_truncation", "no_padding"]
    assert state.constructor == {
        "model_name": MODEL_NAME,
        "specific_model_path": str(cache),
        "local_files_only": True,
        "providers": ["CPUExecutionProvider"],
        "cuda": False,
        "threads": 2,
    }
    documents = provider.embed_documents(["first", "second"])
    assert len(documents) == 2
    assert len(provider.embed_query("query text")) == 384
    assert state.texts == [["document: first", "document: second"], ["query: query text"]]
    assert state.options == [{"batch_size": provider.cfg["batch_size"], "parallel": None}] * 2


def test_overlong_member_rejects_the_entire_batch_before_inference(backend):
    """A short first record must not be embedded before a later record is rejected."""
    provider, state, _, _ = backend
    accepted = " ".join(["token"] * 127)
    provider.embed_documents([accepted])  # One instruction token makes exactly 128.
    state.texts.clear()
    with pytest.raises(module.LocalEmbeddingInputTooLongError):
        provider.embed_documents(["short", accepted + " extra"])
    assert state.texts == []


@pytest.mark.parametrize("vectors", [[], [[1.0]], [[float("nan")] * 384], [[0.0] * 384]])
def test_cpu_backend_output_uses_shared_vector_validation(backend, vectors):
    """Reject wrong counts/dimensions, nonfinite values and zero vectors."""
    provider, state, _, _ = backend
    state.vectors = vectors
    with pytest.raises(ValueError):
        provider.embed_query("synthetic query")


def test_closed_cpu_provider_cannot_run_another_inference(backend):
    """Closing releases the session and subsequent calls fail safely."""
    provider, state, _, _ = backend
    provider.close()
    with pytest.raises(module.LocalEmbeddingUnavailableError):
        provider.embed_query("synthetic query")
    assert state.texts == []


@pytest.mark.parametrize(
    "installed", [None, "unsupported-version", "0.9.0+other", "0.9.0+qm.pillow13.1"]
)
def test_optional_dependency_failure_is_actionable_without_provider_fallback(
    monkeypatch, installed
):
    """Missing or incompatible packages require explicit local-extra installation."""

    def metadata(name):
        if installed is None:
            raise PackageNotFoundError(name)
        return installed

    monkeypatch.setattr(module, "version", metadata)
    with pytest.raises(module.LocalEmbeddingUnavailableError):
        module.require_local_dependencies()


@pytest.mark.parametrize("installed", [FASTEMBED_VERSION, "0.9.0+qm.pillow13"])
def test_optional_dependency_loading_uses_the_pinned_backend(monkeypatch, installed):
    """Load only the supported optional backend and tokenizer classes."""
    embedding_class = object()
    tokenizer_class = object()
    monkeypatch.setattr(module, "version", lambda name: installed)
    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=embedding_class))
    monkeypatch.setitem(sys.modules, "tokenizers", SimpleNamespace(Tokenizer=tokenizer_class))
    assert module.require_local_dependencies() == (embedding_class, tokenizer_class)
