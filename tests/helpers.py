"""Deterministic service fixtures backed by real Qdrant and SQLite components."""

from types import SimpleNamespace

from qdrant_client import QdrantClient

from qdrant_memory.config import DEFAULTS, merge
from qdrant_memory.extraction import Extractor
from qdrant_memory.ledger import Ledger
from qdrant_memory.qdrant_store import QdrantStore
from qdrant_memory.runtime import Runtime


class Embedder:
    """Provide deterministic three-dimensional vectors for behavior contracts."""

    dimensions = 3
    fingerprint = "test-embedding-space-v1"

    def embed_documents(self, texts):
        """Embed fixture texts in input order."""
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text):
        # Fixed, interpretable embedding fixture: three unrelated topics.
        """Map fixture topics to deterministic unit vectors."""
        if "cat" in text.lower():
            return [1.0, 0.0, 0.0]
        if "dog" in text.lower():
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]


class LLM:
    """Return controlled structured LLM responses and capture routing options."""

    def __init__(self, relation="UNRELATED"):
        """Initialize the relation decision and captured-call list."""
        self.calls = []
        self.relation_value = relation

    def complete_structured(self, **kwargs):
        """Return fixture facts or the configured relation without contacting a model."""
        import json

        self.calls.append(kwargs)
        if kwargs["purpose"].endswith("relation"):
            return SimpleNamespace(parsed={"relation": self.relation_value})
        turn = json.loads(kwargs["input"][0]["text"])
        return SimpleNamespace(
            parsed={
                "memories": [{"text": turn["user"], "category": ["preference"], "importance": 0.8}]
            }
        )


def config(**overrides):
    """Merge compact test defaults with scenario-specific settings."""
    return merge(
        merge(
            DEFAULTS,
            {
                "embedding": {"dimensions": 3},
                "write": {"backoff_base_seconds": 0, "backoff_max_seconds": 0},
            },
        ),
        overrides,
    )


def runtime(home, client=None, cfg=None, llm=None, ledger_namespace=None):
    """Build a real Qdrant store and ledger with deterministic injected services."""
    cfg = cfg or config()
    client = client or QdrantClient(":memory:")
    store = QdrantStore(client, cfg, Embedder())
    store.initialize()
    return Runtime(
        cfg,
        store,
        Ledger(home, ledger_namespace or store.collection),
        Extractor(llm or LLM(), cfg["llm"]),
    )
