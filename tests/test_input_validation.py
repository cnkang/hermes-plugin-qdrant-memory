"""Reject empty memories and evaluation samples before storage or metrics."""

from types import SimpleNamespace

import pytest

from qdrant_memory.migration import verify_collection
from qdrant_memory.models import Scope, content_hash, enforce_limits, payload
from scripts.evaluate import evaluate

from .helpers import config, runtime


@pytest.mark.parametrize("text,bound", [("猫", 1), ("é", 1), ("  猫", 2), ("\u3000猫", 3)])
def test_truncation_rejects_normalized_empty_text(text, bound):
    """Reject incomplete UTF-8 and whitespace-only truncated memories."""
    limits = config(limits={"max_text_bytes": bound, "oversize_policy": "truncate"})["limits"]
    with pytest.raises(ValueError, match="nonempty"):
        enforce_limits(payload(text, Scope("user", None), "manual_tool"), limits)


def test_evaluation_rejects_empty_queries_before_writing():
    """Validate query samples before store writes or metric calculations."""
    writes = []
    store = SimpleNamespace(upsert=writes.append)
    with pytest.raises(ValueError, match="queries.*nonempty"):
        evaluate(
            {"memories": [{"id": "one", "text": "cats"}], "queries": []}, store, Scope("u", None)
        )
    assert writes == []


def test_verification_rejects_empty_payload_with_matching_hash(tmp_path):
    """A valid hash cannot certify an empty imported or corrupted memory."""
    rt = runtime(tmp_path)
    try:
        record = rt.add("cats", Scope("u", None))
        rt.store.client.set_payload(
            rt.store.collection,
            {"text": " ", "content_hash": content_hash(" ")},
            points=[record["id"]],
            wait=True,
        )
        result = verify_collection(rt.store)
        assert not result["ok"]
        assert result["invalid_ids"] == [record["id"]]
    finally:
        rt.store.close()
        rt.ledger.close()


def test_complete_truncated_character_and_reject_policy():
    """Preserve complete UTF-8 prefixes and retain oversize rejection."""
    value = payload("猫é", Scope("user", None), "manual_tool")
    limits = config(limits={"max_text_bytes": 3, "oversize_policy": "truncate"})["limits"]
    assert enforce_limits(value, limits)["text"] == "猫"
    limits["oversize_policy"] = "reject"
    with pytest.raises(ValueError, match="max_text_bytes"):
        enforce_limits(value, limits)


@pytest.mark.parametrize("invalid", ["memories", "labels", "unknown", "duplicate", "query"])
def test_evaluation_rejects_invalid_labels_before_writing(invalid):
    """Reject empty samples, unknown labels and duplicate memory identifiers."""
    dataset = {
        "memories": [{"id": "one", "text": "cats"}],
        "queries": [{"query": "cats", "relevant_ids": ["one"]}],
    }
    if invalid == "memories":
        dataset["memories"] = []
    elif invalid == "labels":
        dataset["queries"][0]["relevant_ids"] = []
    elif invalid == "unknown":
        dataset["queries"][0]["relevant_ids"] = ["missing"]
    elif invalid == "duplicate":
        dataset["memories"] *= 2
    else:
        dataset["queries"][0]["query"] = " "
    writes = []
    with pytest.raises(ValueError, match="Dataset"):
        evaluate(dataset, SimpleNamespace(upsert=writes.append), Scope("u", None))
    assert writes == []
