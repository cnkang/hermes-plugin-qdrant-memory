"""Explicit intent and automatic factual adjudication remain separate policies."""

import json
import math
from types import SimpleNamespace

import pytest

from qdrant_memory.models import Scope
from qdrant_memory.tools import dispatch

from .helpers import LLM, runtime


def test_extraction_schema_keeps_strict_provider_objects_closed_and_required():
    """Strict OpenAI-compatible structured output requires every property to be required."""
    from qdrant_memory.extraction import MEMORY_SCHEMA

    item = MEMORY_SCHEMA["properties"]["memories"]["items"]
    assert set(item["properties"]) == set(item["required"])
    assert item["additionalProperties"] is False


@pytest.mark.parametrize("replacement", [True, False])
def test_explicit_replacement_reviews_low_similarity_candidates_in_scope(tmp_path, replacement):
    """A replacement can reach its fact below the cutoff without changing ordinary adds."""
    old = "Project Willow prefers cats"
    updated = "Project Willow prefers dogs"
    statement = "For Project Willow, replace the previous cats preference with dogs."

    class ReplacementLLM(LLM):
        """Classify only a replacement supported by the original user statement."""

        def complete_structured(self, **kwargs):
            """Return a normalized candidate and scoped factual relations."""
            self.calls.append(kwargs)
            value = json.loads(kwargs["input"][0]["text"])
            if kwargs["purpose"].endswith("relation"):
                relation = (
                    "SUPERSEDES"
                    if value["old"] == old and value.get("user_statement") == statement
                    else "UNRELATED"
                )
                return SimpleNamespace(parsed={"relation": relation})
            return SimpleNamespace(
                parsed={
                    "memories": [
                        {
                            "text": updated,
                            "category": [],
                            "importance": 0.8,
                            "replacement_intent": replacement,
                        }
                    ]
                }
            )

    llm = ReplacementLLM()
    rt = runtime(tmp_path, llm=llm)
    scope = Scope("alice", "hermes")
    foreign = Scope("mallory", "hermes")
    try:
        first = rt.add(old, scope)
        rt.add("Project Birch prefers dogs", scope)
        rt.add("foreign dogs preference", foreign)
        assert (
            rt.store.search(updated, scope, top_k=5)[1].score
            < rt.cfg["dedupe"]["similarity_review_threshold"]
        )
        key = rt.ledger.enqueue_event(
            {
                "kind": "turn",
                "scope": scope.as_dict(),
                "source": "conversation",
                "session_id": "s",
                "user": statement,
                "assistant": "ack",
            }
        )
        rt.process_event(key)
        assert rt.store.count(scope) == (2 if replacement else 3)
        assert rt.store.get(first["id"], scope).payload["text"] == (updated if replacement else old)
        relations = [
            json.loads(call["input"][0]["text"])
            for call in llm.calls
            if call["purpose"].endswith("relation")
        ]
        assert all("foreign" not in call["old"] for call in relations)
        assert rt.store.count(foreign) == 1
    finally:
        rt.store.close()
        rt.ledger.close()


@pytest.mark.parametrize("score", [0.92, 0.97, 1.0])
@pytest.mark.parametrize("with_extractor", [False, True])
def test_explicit_add_keeps_distinct_similar_facts(tmp_path, score, with_extractor):
    """Similarity cannot discard explicit intent, even when an LLM reports SAME."""
    llm = LLM("SAME")
    rt = runtime(tmp_path, llm=llm)
    if not with_extractor:
        rt.extractor = None
    scope = Scope("alice", "hermes")
    rt.store.embedder.embed_query = lambda text: (
        [1.0, 0.0, 0.0] if "preferred" in text else [score, math.sqrt(1 - score**2), 0.0]
    )
    try:
        first = dispatch(rt, "qdrant_memory_add", {"text": "cats preferred"}, scope, "s")
        assert rt.store.search("cats avoided", scope)[0].score == pytest.approx(score)
        second = dispatch(rt, "qdrant_memory_add", {"text": "cats avoided"}, scope, "s")
        assert first["action"] == second["action"] == "ADD"
        assert first["id"] != second["id"]
        duplicate = rt.add("  cats   preferred  ", scope)
        assert duplicate == {"action": "SKIP", "id": first["id"]}
        for other in (Scope("bob", "hermes"), Scope("alice", "other")):
            assert rt.add("cats preferred", other)["id"] != first["id"]
            assert rt.store.count(other) == 1
        assert rt.store.count(scope) == 2
        assert not llm.calls
    finally:
        rt.store.close()
        rt.ledger.close()


@pytest.mark.parametrize(
    "relation,count,texts",
    [
        ("SAME", 1, {"cats preferred"}),
        ("SUPERSEDES", 1, {"cats avoided"}),
        ("CONFLICT", 2, {"cats preferred", "cats avoided"}),
        ("UNRELATED", 2, {"cats preferred", "cats avoided"}),
    ],
)
def test_automatic_events_retain_factual_relations(tmp_path, relation, count, texts):
    """Real extraction and durable event processing still adjudicate similar facts."""
    llm = LLM(relation)
    rt = runtime(tmp_path, llm=llm)
    scope = Scope("alice", "hermes")
    try:
        first = rt.add("cats preferred", scope)
        key = rt.ledger.enqueue_event(
            {
                "kind": "turn",
                "scope": scope.as_dict(),
                "source": "conversation",
                "session_id": "s",
                "user": "cats avoided",
                "assistant": "ack",
            }
        )
        rt.process_event(key)
        assert rt.store.count(scope) == count
        hits = rt.store.search("cats", scope)
        assert {h.payload["text"] for h in hits} == texts
        if relation == "CONFLICT":
            conflict = next(h for h in hits if h.payload["text"] == "cats avoided")
            assert conflict.payload["metadata"]["conflicts_with"] == first["id"]
        assert any(c["purpose"] == "qdrant-memory.relation" for c in llm.calls)
        assert rt.ledger.row("events", key)["status"] == "COMMITTED"
    finally:
        rt.store.close()
        rt.ledger.close()
