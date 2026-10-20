"""Explicit intent and automatic factual adjudication remain separate policies."""

import json
import math
import os
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


@pytest.mark.parametrize("real_model", [False, True])
def test_multi_subject_replacements_update_only_their_own_records(
    tmp_path, monkeypatch, real_model
):
    """A turn that replaces two facts cannot lend one fact's intent to the other."""
    if real_model and os.environ.get("QDRANT_MEMORY_TEST_RELATION_MODEL") != "1":
        pytest.skip("Set QDRANT_MEMORY_TEST_RELATION_MODEL=1 for the installed local-model probe")
    old = ["Project Willow cache TTL is 5 minutes.", "Project Birch deployment region is US."]
    new = ["Project Willow cache TTL is 10 minutes.", "Project Birch deployment region is EU."]
    statement = (
        "Change Project Willow cache TTL from 5 minutes to 10 minutes. "
        "Change Project Birch deployment region from US to EU."
    )
    delegate = None
    if real_model:
        import httpx

        # This test facade uses only a local model, without inherited credentials.
        delegate = httpx.Client(trust_env=False, timeout=30)

    class TwoFactLLM(LLM):
        """Fix extraction inputs while exercising real relation and mutation paths."""

        def complete_structured(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["purpose"].endswith("relation"):
                if delegate is not None:
                    # Explicitly disable thinking through the local test API.
                    # https://docs.ollama.com/capabilities/thinking
                    response = delegate.post(
                        "http://127.0.0.1:11434/api/chat",
                        json={
                            "model": "qwen3:8b",
                            "messages": [
                                {"role": "system", "content": kwargs["instructions"]},
                                {"role": "user", "content": kwargs["input"][0]["text"]},
                            ],
                            "format": kwargs["json_schema"],
                            "think": False,
                            "stream": False,
                            "options": {"temperature": 0, "num_predict": 800},
                        },
                    )
                    response.raise_for_status()
                    result = response.json()
                    assert result["done_reason"] == "stop", "Local relation output was truncated"
                    return SimpleNamespace(parsed=json.loads(result["message"]["content"]))
                value = json.loads(kwargs["input"][0]["text"])
                relation = "UNRELATED"
                if (value["old"], value["new"]) in zip(old, new):
                    relation = "SUPERSEDES" if "user_statement" in value else "CONFLICT"
                return SimpleNamespace(parsed={"relation": relation})
            return SimpleNamespace(
                parsed={
                    "memories": [
                        {
                            "text": text,
                            "category": [],
                            "importance": 0.8,
                            "replacement_intent": True,
                        }
                        for text in new
                    ]
                }
            )

    llm = TwoFactLLM()
    rt = runtime(tmp_path, llm=llm)
    scope = Scope("alice", "hermes")
    try:
        originals = [rt.add(text, scope)["id"] for text in old]
        search = rt.store.search

        def unrelated_first(text, *args, **kwargs):
            hits = search(text, *args, **kwargs)
            own_id = originals[new.index(text)]
            return sorted(hits, key=lambda hit: str(hit.id) == own_id)

        monkeypatch.setattr(rt.store, "search", unrelated_first)
        key = rt.ledger.enqueue_event(
            {
                "kind": "turn",
                "scope": scope.as_dict(),
                "source": "conversation",
                "session_id": "two-subjects",
                "user": statement,
                "assistant": "ack",
            }
        )
        rt.process_event(key)
        assert rt.store.count(scope) == 2
        assert [rt.store.get(identifier, scope).payload["text"] for identifier in originals] == new
        assert rt.ledger.row("events", key)["status"] == "COMMITTED"
        relation_calls = [call for call in llm.calls if call["purpose"].endswith("relation")]
        assert 4 <= len(relation_calls) <= 6
        for call in relation_calls:
            value = json.loads(call["input"][0]["text"])
            if (value["old"], value["new"]) not in zip(old, new):
                assert "user_statement" not in value
    finally:
        rt.store.close()
        rt.ledger.close()
        if delegate is not None:
            delegate.close()


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
                relation = "UNRELATED"
                if value["old"] == old:
                    relation = (
                        "SUPERSEDES" if value.get("user_statement") == statement else "CONFLICT"
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
        hits = rt.store.search(updated, scope, top_k=5)
        old_hit = next(hit for hit in hits if str(hit.id) == first["id"])
        assert old_hit.score < rt.cfg["dedupe"]["similarity_review_threshold"]
        assert str(hits[0].id) != first["id"]
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
