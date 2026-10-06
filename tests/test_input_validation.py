"""Reject empty memories and evaluation samples before storage or metrics."""

from types import SimpleNamespace

import pytest

from qdrant_memory.models import Scope, enforce_limits, payload
from scripts.evaluate import evaluate

from .helpers import config


@pytest.mark.parametrize("text,bound", [("猫", 1), ("  猫", 2), ("\u3000猫", 3)])
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
