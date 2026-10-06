"""Configuration, identity, embedding wire format and retry contracts."""

import json

import httpx
import pytest

from qdrant_memory.config import DEFAULTS, fingerprint, load_config, merge
from qdrant_memory.config_schema import save_config
from qdrant_memory.embedding import HTTPEmbeddingProvider, build_embedder
from qdrant_memory.models import Scope, content_hash, enforce_limits, payload, point_id
from qdrant_memory.retry import run_with_retry, safe_error


def test_scoped_config_and_secret_safe_save(tmp_path, monkeypatch):
    """Verify scoped config and secret safe save."""
    monkeypatch.setattr(
        "qdrant_memory.config.secret",
        lambda name: {"QDRANT_URL": "https://cloud.example", "QDRANT_API_KEY": "sentinel-key"}.get(
            name
        ),
    )
    path = tmp_path / "qdrant-memory.json"
    path.write_text(
        json.dumps(
            {
                "qdrant": {
                    "mode": "cloud",
                    "url": "https://explicit.example",
                    "api_key": "explicit-key",
                }
            }
        )
    )
    cfg = load_config(tmp_path)
    assert cfg["qdrant"]["url"] == "https://explicit.example"
    assert cfg["qdrant"]["api_key"] == "explicit-key"
    save_config({"api_key": "never-save-me", "embedding_api_key": "nor-this"}, tmp_path)
    saved = json.loads(path.read_text())
    assert "api_key" not in saved["qdrant"]
    assert "api_key" not in saved["embedding"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_config(tmp_path)["qdrant"]["api_key"] == "sentinel-key"


def test_hash_identity_and_payload_limits():
    """Verify hash identity and payload limits."""
    a, b = Scope("alice", "hermes"), Scope("bob", "hermes")
    assert content_hash(" hello\nworld ") == content_hash("hello world")
    reconstructed = Scope(**json.loads(json.dumps(a.as_dict())))
    assert point_id(a, "mem0", "id") == point_id(reconstructed, "mem0", "id")
    assert point_id(a, "mem0", "id") != point_id(b, "mem0", "id")
    value = payload("é" * 10, a, "manual_tool")
    limits = merge(DEFAULTS["limits"], {"max_text_bytes": 5})
    with pytest.raises(ValueError):
        enforce_limits(value, limits)
    truncated = enforce_limits(value, {**limits, "oversize_policy": "truncate"})
    assert truncated["text"] == "éé"
    assert truncated["content_hash"] == content_hash("éé")


@pytest.mark.parametrize("provider", ["ollama", "openai-compatible"])
def test_embedding_wire_contract(provider, monkeypatch):
    """Verify embedding wire contract."""
    monkeypatch.setattr("qdrant_memory.embedding.secret", lambda name: None)
    requests = []

    def handler(request):
        """Serve controlled embedding responses and capture outgoing requests."""
        requests.append(request)
        if provider == "ollama":
            return httpx.Response(200, json={"embeddings": [[1, 0], [0, 1]]})
        return httpx.Response(
            200,
            json={"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]},
        )

    cfg = merge(
        DEFAULTS["embedding"],
        {
            "provider": provider,
            "dimensions": 2,
            "base_url": "http://example.test/v1" if provider != "ollama" else "http://example.test",
        },
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    embedder = HTTPEmbeddingProvider(cfg, client)
    assert embedder.embed_documents(["cat", "dog"]) == [[1, 0], [0, 1]]
    request = requests[0]
    body = json.loads(request.content)
    assert "dimensions" not in body
    assert request.url.path == ("/api/embed" if provider == "ollama" else "/v1/embeddings")
    assert provider != "ollama" or body["truncate"] is False
    client.close()


def test_embedding_dimensions_and_fallback(monkeypatch):
    """Verify embedding dimensions and fallback."""
    monkeypatch.setattr("qdrant_memory.embedding.secret", lambda name: None)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"embeddings": [[1, 0, 0]]})
        )
    )
    embedder = HTTPEmbeddingProvider(merge(DEFAULTS["embedding"], {"dimensions": 2}), client)
    with pytest.raises(ValueError, match="dimension"):
        embedder.embed_query("query")
    client.close()
    cfg = merge(DEFAULTS, {"embedding": {"mode": "inherit", "inherit_fallback": {"dimensions": 2}}})
    fallback = build_embedder(None, cfg)
    assert fallback.dimensions == 2
    fallback.close()
    assert fingerprint(cfg["embedding"]) != fingerprint({**cfg["embedding"], "model": "changed"})


@pytest.mark.parametrize("code,attempts", [(429, 3), (503, 3), (401, 1), (403, 1), (400, 1)])
def test_retry_classification_and_redaction(code, attempts):
    """Verify retry classification and redaction."""
    calls = []

    def action():
        """Fail transiently before allowing the retry contract to succeed."""
        calls.append(1)
        response = httpx.Response(
            code, request=httpx.Request("GET", "https://example.test"), text="sentinel-secret"
        )
        response.raise_for_status()

    with pytest.raises(httpx.HTTPStatusError) as error:
        run_with_retry(
            action, {"max_attempts": 3, "backoff_base_seconds": 0, "backoff_max_seconds": 0}
        )
    assert len(calls) == attempts
    assert "sentinel-secret" not in json.dumps(safe_error(error.value))
