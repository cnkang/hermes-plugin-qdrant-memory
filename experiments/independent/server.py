"""Experiment-only REST adapter, shared by both retrieval paths."""

import hashlib
import math
import uuid

import httpx

MODEL = "qwen3-embedding:4b"
IMAGE = "qdrant/qdrant@sha256:9bfc89f9914fc14876a88c05270685921a55b8877b254a98bcc1cc2f0a424cd2"
CONFIG = dict(
    model=MODEL,
    image=IMAGE,
    top_k=8,
    dense_candidates=24,
    sparse_candidates=24,
    rrf_k=60,
    tokenizer="multilingual",
    min_token_len=2,
    max_token_len=15,
    lowercase=True,
    enable_hnsw=True,
    score_threshold=None,
    query_instruction="",
    document_instruction="",
    bootstrap_draws=10000,
    bootstrap_seed=152,
    latency_bootstrap_draws=2000,
    warmup_passes=2,
    model_digest="df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907",
)
PATHS = ("dense_baseline", "hybrid_multilingual_rrf")


def point_id(mid):
    """Map fictional evidence IDs to stable UUIDs."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "fictional-bm25-independent/" + mid))


def scope_filter(scope):
    """Constrain user and exact agent scope."""
    user, agent = scope
    condition = (
        {"is_null": {"key": "agent_id"}}
        if agent is None
        else {"key": "agent_id", "match": {"value": agent}}
    )
    return {"must": [{"key": "user_id", "match": {"value": user}}, condition]}


class Embedding:
    """Request vectors only from the pinned real model."""

    def __init__(self, url):
        """Bind an isolated endpoint and validate experiment identity."""
        self.http = httpx.Client(base_url=url, timeout=180, trust_env=False)
        tags = self.http.get("/api/tags").raise_for_status().json()["models"]
        model = next((m for m in tags if m["name"] == MODEL), None)
        if not model:
            raise LookupError(f"UNVERIFIED: required real model {MODEL} unavailable")
        if model["digest"] != CONFIG["model_digest"]:
            raise LookupError("UNVERIFIED: installed model digest differs from frozen model")
        self.metadata = dict(
            name=MODEL,
            digest=model["digest"],
            ollama_version=self.http.get("/api/version").raise_for_status().json(),
        )

    def embed(self, text):
        """Request and validate a real model vector."""
        r = self.http.post(
            "/api/embed",
            json={"model": MODEL, "input": text, "truncate": False, "keep_alive": "30m"},
        )
        r.raise_for_status()
        vector = r.json()["embeddings"][0]
        assert len(vector) == 2560 and all(math.isfinite(x) for x in vector), (
            "invalid real embedding"
        )
        return vector


def fixture_vector(text):
    """Generate deterministic lifecycle-only vectors."""
    raw = hashlib.sha256(text.encode()).digest()
    return [(x - 127.5) / 127.5 for x in raw]


class Server:
    """Share one temporary collection and REST transport."""

    def __init__(self, url, collection):
        """Bind an isolated endpoint and validate experiment identity."""
        if not collection.startswith("bm25_ind_") or not collection.replace("_", "").isalnum():
            raise ValueError("only temporary bm25_ind_* collections are allowed")
        self.http = httpx.Client(base_url=url, timeout=180, trust_env=False)
        self.base = f"/collections/{collection}"
        self.collection = collection

    def request(self, method, suffix="", **kwargs):
        """Send a collection request and require success."""
        r = self.http.request(method, self.base + suffix, **kwargs)
        r.raise_for_status()
        return r.json().get("result")

    def create(self, dimensions):
        """Create a fresh collection and frozen BM25 index."""
        # Never overwrite an existing collection, even an experiment collection.
        self.request("PUT", json={"vectors": {"dense": {"size": dimensions, "distance": "Cosine"}}})
        self.request(
            "PUT",
            "/index?wait=true",
            json={
                "field_name": "text",
                "field_schema": {
                    "type": "text",
                    **{
                        k: CONFIG[k]
                        for k in (
                            "tokenizer",
                            "min_token_len",
                            "max_token_len",
                            "lowercase",
                            "enable_hnsw",
                        )
                    },
                    "scoring": {"type": "bm25"},
                },
            },
        )

    def upsert(self, memory, vector):
        """Replace text and vector for an evidence ID."""
        self.request(
            "PUT",
            "/points?wait=true",
            json={
                "points": [
                    {
                        "id": point_id(memory["id"]),
                        "vector": {"dense": vector},
                        "payload": {
                            "memory_id": memory["id"],
                            "text": memory["text"],
                            "user_id": memory["scope"][0],
                            "agent_id": memory["scope"][1],
                        },
                    }
                ]
            },
        )

    def delete(self, mid):
        """Wait for deletion of an experiment point."""
        self.request("POST", "/points/delete?wait=true", json={"points": [point_id(mid)]})

    def search(self, query, vector, scope, path):
        """Run a fixed path and verify all returned scopes."""
        filt = scope_filter(scope)
        common = dict(filter=filt, with_payload=True, with_vector=False)
        if path == PATHS[0]:
            body = dict(query=vector, using="dense", limit=24, **common)
        elif path == PATHS[1]:
            body = dict(
                prefetch=[
                    dict(query=vector, using="dense", limit=24, filter=filt),
                    dict(query={"text": query}, using="text", limit=24, filter=filt),
                ],
                query={"rrf": {"k": 60}},
                limit=8,
                **common,
            )
        elif path == "sparse_fixture":
            body = dict(query={"text": query}, using="text", limit=24, **common)
        else:
            raise ValueError(path)
        points = self.request("POST", "/points/query", json=body)["points"]
        for p in points:
            assert [p["payload"]["user_id"], p["payload"].get("agent_id")] == scope, (
                "CROSS_SCOPE_LEAK"
            )
        return [p["payload"]["memory_id"] for p in points][:8]

    def close(self):
        """Release the shared HTTP connection pool."""
        self.http.close()
