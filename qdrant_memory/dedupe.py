"""Similarity selects candidates; factual relations decide mutation."""
from .models import content_hash


def decide(text, scope, store, extractor, cfg):
    exact = store.lookup_hash(scope, content_hash(text))
    if exact:
        return "SKIP", str(exact.id), {}
    hits = store.search(text, scope, top_k=5)
    if not hits or hits[0].score < cfg["similarity_review_threshold"]:
        return "ADD", None, {}
    top = hits[0]
    relation = extractor.relation(top.payload["text"], text)
    if relation == "SAME":
        return "SKIP", str(top.id), {}
    if relation == "SUPERSEDES":
        return "UPDATE", str(top.id), {}
    if relation == "CONFLICT":
        return "ADD", None, {"conflicts_with": str(top.id)}
    return "ADD", None, {}
