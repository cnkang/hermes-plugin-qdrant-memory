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
    # Only explicit replacement intent broadens the existing bounded review set.
    # Every candidate remains in the same authorized scope and must pass relation review.
    candidates = hits if replacement else hits[:1]
    review_kwargs = {"user_statement": replacement_context} if replacement else {}
    return _review_candidates(candidates, text, extractor, review_kwargs)


def _review_candidates(candidates, text, extractor, review_kwargs):
    """Review candidates in order and return the first decisive mutation.

    A first contradiction is carried as a linked conflict when no candidate
    supersedes or duplicates the new text.
    """
    conflict = None
    for top in candidates:
        relation = extractor.relation(top.payload["text"], text, **review_kwargs)
        if relation == "SAME":
            return "SKIP", str(top.id), {}
        if relation == "SUPERSEDES":
            return "UPDATE", str(top.id), {}
        if relation == "CONFLICT" and conflict is None:
            conflict = str(top.id)
    return "ADD", None, {"conflicts_with": conflict} if conflict is not None else {}
