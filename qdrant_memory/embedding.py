"""Ollama and OpenAI-compatible embeddings; no LLM credential handling."""
import math
from typing import Protocol
from .config import embedding_config, fingerprint, secret


class EmbeddingProvider(Protocol):
    dimensions: int
    fingerprint: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def validate_vectors(vectors, count, dimensions):
    if not isinstance(vectors, list) or len(vectors) != count:
        raise ValueError("Embedding response count mismatch")
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != dimensions:
            raise ValueError("Embedding dimension mismatch")
        if not all(isinstance(v, (float, int)) and math.isfinite(v) for v in vector) or not any(vector):
            raise ValueError("Invalid embedding vector")
    return vectors


class HTTPEmbeddingProvider:
    def __init__(self, cfg, client=None):
        import httpx
        self.cfg = cfg
        self.dimensions = int(cfg["dimensions"])
        self.fingerprint = fingerprint(cfg)
        key = cfg.get("api_key") or secret(cfg.get("api_key_env", "EMBEDDING_API_KEY"))
        self.client = client or httpx.Client(timeout=cfg["timeout_seconds"],
                                             headers={"Authorization": f"Bearer {key}"} if key else {})

    def _embed(self, texts):
        base = self.cfg["base_url"].rstrip("/")
        body = {"model": self.cfg["model"], "input": texts}
        if self.cfg["provider"] == "ollama":
            url = base + "/api/embed"
            # Reject provider-side silent truncation of durable facts.
            body["truncate"] = False
        else:
            url = base + ("/embeddings" if base.endswith("/v1") else "/v1/embeddings")
            if self.cfg.get("send_dimensions"):
                body["dimensions"] = self.dimensions
        response = self.client.post(url, json=body)
        response.raise_for_status()
        data = response.json()
        if self.cfg["provider"] == "ollama":
            vectors = data["embeddings"]
        else:
            entries = sorted(data["data"], key=lambda item: item["index"])
            if [item["index"] for item in entries] != list(range(len(texts))):
                raise ValueError("Embedding response indices mismatch")
            vectors = [item["embedding"] for item in entries]
        return validate_vectors(vectors, len(texts), self.dimensions)

    def embed_documents(self, texts):
        prefix = self.cfg.get("document_instruction", "")
        result = []
        for start in range(0, len(texts), int(self.cfg["batch_size"])):
            result.extend(self._embed([prefix + t for t in texts[start:start + int(self.cfg["batch_size"])]]))
        return result

    def embed_query(self, text):
        return self._embed([self.cfg.get("query_instruction", "") + text])[0]

    def close(self):
        self.client.close()


def build_embedder(ctx, cfg):
    if cfg["embedding"]["mode"] == "inherit":
        host = getattr(ctx, "embedding", None)
        if host is not None:
            # Future facade must explicitly advertise its pipeline identity.
            if not all(hasattr(host, k) for k in ("dimensions", "fingerprint", "embed_documents", "embed_query")):
                raise ValueError("Host embedding facade does not satisfy the plugin contract")
            return host
    return HTTPEmbeddingProvider(embedding_config(cfg))
