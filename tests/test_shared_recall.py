"""Native shared recall never reuses cached authorization or writes shared data."""

import json
import threading
import time

import pytest

from qdrant_memory.config import validate
from qdrant_memory.shared_recall import SharedRecall

from .helpers import config
from .test_provider import provider


def settings(**extra):
    """Return an explicitly authorized project reader configuration."""
    return {
        "enabled": True,
        "spaces": [
            {
                "space_id": "alpha",
                "visibility": "project",
                "url": "http://127.0.0.1:8765",
                "token_env": "ALPHA_TOKEN",
            }
        ],
        **extra,
    }


def test_fresh_shared_delivery_and_no_private_persistence(tmp_path, monkeypatch):
    """A revoked/deleted result must not survive in the provider recall cache."""
    calls = []
    rows = [
        {
            "id": "one",
            "text": "Project Alpha uses SQLite WAL.",
            "score": 0.9,
            "shared_protocol": "qdrant-memory-shared-v1",
            "space_id": "alpha",
            "visibility": "project",
            "version": "v1",
            "created_by": "device-a",
        }
    ]

    def call(url, token, name, arguments, **kwargs):
        calls.append(name)
        return {"memories": list(rows)}

    monkeypatch.setattr("qdrant_memory.shared_client.call", call)
    p = provider(tmp_path)
    try:
        p._shared_recall = SharedRecall(settings(), secret_reader=lambda _: "x" * 40)
        p.sync_turn("private cats", "ack")
        assert p.wait_idle()
        p.queue_prefetch("cats")
        assert p.wait_idle()
        recalled = p.prefetch("cats")
        assert "private cats" in recalled and "SQLite WAL" in recalled
        assert "alpha" in recalled and "v1" in recalled and "device-a" in recalled
        rows.clear()
        assert "SQLite WAL" not in p.prefetch("cats")
        assert p.store.count() == 1
        assert set(calls) == {"qdrant_memory_search"}
        p.on_turn_start(2, "", author_id="another-user")
        before = len(calls)
        assert "SQLite WAL" not in p.prefetch("cats")
        assert len(calls) == before
    finally:
        p.shutdown()


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": "true"},
        {"spaces": []},
        {"limit": True},
        {"timeout_seconds": 11},
        {
            "spaces": [
                {
                    "space_id": "x",
                    "visibility": "private",
                    "url": "http://127.0.0.1:1",
                    "token_env": "ALPHA_TOKEN",
                }
            ]
        },
        {
            "spaces": [
                {
                    "space_id": "x",
                    "visibility": "project",
                    "url": "https://evil.example",
                    "token_env": "ALPHA_TOKEN",
                }
            ]
        },
    ],
)
def test_invalid_shared_settings_rejected(change):
    """Reject settings before opening a private store or sending credentials."""
    with pytest.raises(ValueError):
        validate(config(shared_read=settings(**change)))


def test_partial_failure_and_sanitized_diagnostics(monkeypatch):
    """One denied space cannot suppress successful authorized results."""
    cfg = settings()
    cfg["spaces"].append({**cfg["spaces"][0], "space_id": "beta"})
    recall = SharedRecall(cfg, secret_reader=lambda _: "x" * 40)

    def call(url, token, name, arguments, **kwargs):
        raise ValueError("SECRET-ERROR")

    monkeypatch.setattr("qdrant_memory.shared_client.call", call)
    assert recall.search("query") == []
    assert recall.failures == 2
    assert "x" * 40 not in repr(recall)


def test_tool_opt_in_preserves_private_result(tmp_path, monkeypatch):
    """Only search can include a separate structured shared result."""
    p = provider(tmp_path)
    try:
        p._shared_recall = SharedRecall(
            settings(include_tool_search=True), secret_reader=lambda _: "x" * 40
        )
        monkeypatch.setattr(p._shared_recall, "search", lambda _, **kwargs: [{"text": "WAL"}])
        result = json.loads(p.handle_tool_call("qdrant_memory_search", {"query": "Alpha"}))
        assert result["memories"] == [] and result["shared"] == [{"text": "WAL"}]
    finally:
        p.shutdown()


def test_slow_shared_read_has_bounded_delivery_and_discards_late_result(tmp_path, monkeypatch):
    """An outstanding read cannot queue or later inject stale shared results."""
    release = threading.Event()
    p = provider(tmp_path)
    try:
        p._shared_recall = SharedRecall(settings(), secret_reader=lambda _: "x" * 40)

        def slow(*args):
            release.wait(2)
            return [{"text": "stale shared"}]

        monkeypatch.setattr(p._shared_recall, "_search", slow)
        start = time.monotonic()
        assert p.prefetch("cats") == ""
        assert time.monotonic() - start < 0.5
        assert p.prefetch("cats") == ""  # bounded backpressure, no queue
        release.set()
        assert "stale shared" not in str(p._cache)
    finally:
        release.set()
        p.shutdown()
