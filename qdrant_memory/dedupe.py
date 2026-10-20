"""Similarity selects candidates; factual relations decide mutation."""

from .models import content_hash


def decide_explicit(text, scope, store):
    """Deduplicate authorized explicit writes only by canonical hash in scope."""
    exact = store.lookup_hash(scope, content_hash(text))
    if exact:
        return "SKIP", str(exact.id), {}
    return "ADD", None, {}


def decide(text, scope, store, extractor, cfg, filters=None, replacement_context=None):
    """Return ADD, UPDATE or SKIP after scoped hash and factual relation checks.

    Similarity only selects a review candidate. A high score alone never authorizes
    replacement; unresolved contradictions produce a separate linked memory.
    """
    kwargs = {"filters": filters} if filters else {}
    exact = store.lookup_hash(scope, content_hash(text), **kwargs)
    if exact:
        return "SKIP", str(exact.id), {}
    hits = store.search(text, scope, top_k=5, **kwargs)
    replacement = isinstance(replacement_context, str) and bool(replacement_context.strip())
    if not hits or (not replacement and hits[0].score < cfg["similarity_review_threshold"]):
        return "ADD", None, {}
    conflict = None
    # Only explicit replacement intent broadens the existing bounded review set.
    # Every candidate remains in the same authorized scope and must pass relation review.
    for top in hits if replacement else hits[:1]:
        relation = extractor.relation(
            top.payload["text"],
            text,
            **({"user_statement": replacement_context} if replacement else {}),
        )
        if relation == "SAME":
            return "SKIP", str(top.id), {}
        if relation == "SUPERSEDES":
            return "UPDATE", str(top.id), {}
        if relation == "CONFLICT" and conflict is None:
            conflict = str(top.id)
    return "ADD", None, {"conflicts_with": conflict} if conflict is not None else {}
