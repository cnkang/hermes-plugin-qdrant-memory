"""Reject empty memories and evaluation samples before storage or metrics."""

from types import SimpleNamespace

import pytest

from qdrant_memory.migration import verify_collection
from qdrant_memory.models import Scope, content_hash, enforce_limits, payload
from qdrant_memory.shared_service import SharedOwner
from qdrant_memory.standalone import call_tool
from qdrant_memory.tools import MAX_ARGUMENT_BYTES, dispatch, validate_arguments
from scripts.evaluate import evaluate

from .helpers import config, runtime


@pytest.mark.parametrize("text", ["a" * 65537, "猫" * 21846])
@pytest.mark.parametrize(
    "operation,field", [("search", "query"), ("add", "text"), ("delete", "id")]
)
def test_tool_bounds_precede_runtime_access(text, operation, field):
    """All entry paths reject oversized strings before touching runtime resources."""
    name, args = "qdrant_memory_" + operation, {field: text}
    with pytest.raises(ValueError, match="byte limit"):
        dispatch(None, name, args, Scope("u", None), "test")
    with pytest.raises(ValueError, match="byte limit"):
        call_tool(None, name, args)
    shared_args = args if operation == "search" else {**args, "request_id": "bounded"}
    if operation == "delete":
        shared_args["expected_version"] = "version"
    with pytest.raises(ValueError, match="byte limit"):
        SharedOwner._validate_arguments(name, shared_args)


def test_tool_bounds_accept_exact_utf8_limit_and_reject_scope_override():
    """Accept the boundary in bytes while keeping extra fields unauthorized."""
    args = {"query": "猫" * (MAX_ARGUMENT_BYTES // 3) + "a"}
    validate_arguments("qdrant_memory_search", args)
    with pytest.raises(ValueError, match="Unexpected"):
        validate_arguments("qdrant_memory_search", {**args, "user_id": "other"})


@pytest.mark.parametrize("text,bound", [("猫", 1), ("é", 1), ("  猫", 2), ("\u3000猫", 3)])
def test_truncation_rejects_normalized_empty_text(text, bound):
    """Reject incomplete UTF-8 and whitespace-only truncated memories."""
    limits = config(limits={"max_text_bytes": bound, "oversize_policy": "truncate"})["limits"]
    record = payload(text, Scope("user", None), "manual_tool")
    with pytest.raises(ValueError, match="nonempty"):
        enforce_limits(record, limits)


def test_evaluation_rejects_empty_queries_before_writing():
    """Validate query samples before store writes or metric calculations."""
    writes = []
    store = SimpleNamespace(upsert=writes.append)
    dataset = {"memories": [{"id": "one", "text": "cats"}], "queries": []}
    scope = Scope("u", None)
    with pytest.raises(ValueError, match="queries.*nonempty"):
        evaluate(dataset, store, scope)
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


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_verification_rejects_non_integer_schema_version(tmp_path, schema_version):
    """Reject booleans and floats that compare equal to schema version 1."""
    rt = runtime(tmp_path)
    try:
        record = rt.add("cats", Scope("u", None))
        rt.store.client.set_payload(
            rt.store.collection,
            {"schema_version": schema_version},
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
    store = SimpleNamespace(upsert=writes.append)
    scope = Scope("u", None)
    with pytest.raises(ValueError, match="Dataset"):
        evaluate(dataset, store, scope)
    assert writes == []
