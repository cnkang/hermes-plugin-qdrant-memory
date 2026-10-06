from types import SimpleNamespace
from qdrant_client import QdrantClient
from qdrant_memory.config import DEFAULTS, merge
from qdrant_memory.ledger import Ledger
from qdrant_memory.qdrant_store import QdrantStore
from qdrant_memory.runtime import Runtime
from qdrant_memory.extraction import Extractor


class Embedder:
    dimensions = 3
    fingerprint = "test-embedding-space-v1"

    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text):
        # Fixed, interpretable embedding fixture: three unrelated topics.
        if "cat" in text.lower():
            return [1.0, 0.0, 0.0]
        if "dog" in text.lower():
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]


class LLM:
    def __init__(self, relation="UNRELATED"):
        self.calls = []
        self.relation_value = relation

    def complete_structured(self, **kwargs):
        import json
        self.calls.append(kwargs)
        if kwargs["purpose"].endswith("relation"):
            return SimpleNamespace(parsed={"relation": self.relation_value})
        turn = json.loads(kwargs["input"][0]["text"])
        return SimpleNamespace(parsed={"memories": [{"text": turn["user"], "category": ["preference"], "importance": 0.8}]})


def config(**overrides):
    return merge(merge(DEFAULTS, {"embedding": {"dimensions": 3},
                               "write": {"backoff_base_seconds": 0, "backoff_max_seconds": 0}}), overrides)


def runtime(home, client=None, cfg=None, llm=None):
    cfg = cfg or config()
    client = client or QdrantClient(":memory:")
    store = QdrantStore(client, cfg, Embedder())
    store.initialize()
    return Runtime(cfg, store, Ledger(home, store.collection), Extractor(llm or LLM(), cfg["llm"]))
