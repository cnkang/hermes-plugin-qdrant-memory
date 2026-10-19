"""One Native Provider hosts the Owner and uses the host extractor for shared events."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from qdrant_memory.provider import QdrantMemoryProvider
from qdrant_memory.shared_client import call

from .helpers import LLM, Embedder, config


def test_native_owner_lifecycle_project_handoff_and_private_isolation(tmp_path, monkeypatch):
    """Actual Native turn hooks write through one Runtime; a second client recalls."""
    tokens = {name: name[0] * 40 for name in ("native", "project", "reader")}
    clients = []
    for name, token in tokens.items():
        clients.append(
            {
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "principal": name,
                "owner_id": "hermes-user",
                "device_id": name,
                "logical_agent_id": "hermes" if name != "reader" else "pi",
                "space_id": "hermes-private" if name == "native" else "alpha",
                "visibility": "private" if name == "native" else "project",
                "permission": "read" if name == "reader" else "write",
            }
        )
    clients[1]["capture_rule_id"] = "explicit-user-approved-alpha"
    policy = tmp_path / "grants.json"
    policy.write_text(json.dumps({"clients": clients}))
    policy.chmod(0o600)
    for name, token in tokens.items():
        monkeypatch.setenv("QM_" + name.upper(), token)
    cfg = config(
        native_owner={
            "enabled": True,
            "policy": str(policy),
            "port": 0,
            "private_token_env": "QM_NATIVE",
            "shared_capture_token_envs": ["QM_PROJECT"],
        }
    )
    p = QdrantMemoryProvider(
        plugin_context=SimpleNamespace(llm=LLM()), embedder=Embedder(), overrides=cfg
    )
    p.initialize("first", hermes_home=str(tmp_path))
    try:
        assert p.wait_idle()
        p.on_turn_start(1, "Project Alpha uses SQLite WAL.")
        p.sync_turn("Project Alpha uses SQLite WAL.", "ack")
        assert p.wait_idle()
        url = p._native_owner.url
        rows = call(url, tokens["reader"], "qdrant_memory_search", {"query": "Alpha"})["memories"]
        assert len(rows) == 1 and rows[0]["text"] == "Project Alpha uses SQLite WAL."
        assert rows[0]["capture_rule_id"] == "explicit-user-approved-alpha"
        private = call(url, tokens["native"], "qdrant_memory_search", {"query": "Alpha"})[
            "memories"
        ]
        assert len(private) == 1 and private[0]["visibility"] == "private"
        assert private[0]["id"] != rows[0]["id"]
        assert p.runtime is p._native_owner.owner.runtime
        p.queue_prefetch("Alpha")
        assert p.wait_idle()
        assert "SQLite WAL" in p.prefetch("Alpha")
        deleted = call(
            url,
            tokens["native"],
            "qdrant_memory_delete",
            {
                "id": private[0]["id"],
                "expected_version": private[0]["version"],
                "request_id": "remote-delete",
            },
        )
        assert deleted["action"] == "DELETE"
        assert p.prefetch("Alpha") == ""
        forged = call(
            url,
            tokens["reader"],
            "qdrant_memory_search",
            {"query": "Alpha", "space_id": "hermes-private"},
        )
        assert "error" in forged
        clients[1]["revoked"] = True
        policy.write_text(json.dumps({"clients": clients}))
        p.sync_turn("dogs private only", "ack")
        assert p.wait_idle()
        assert p._native_owner.owner.blocked is False
        assert p._native_owner.denied_captures == 1
        assert any(
            row["text"] == "dogs private only"
            for row in call(url, tokens["native"], "qdrant_memory_search", {"query": "dogs"})[
                "memories"
            ]
        )
        assert not any(
            row["text"] == "dogs private only"
            for row in call(url, tokens["reader"], "qdrant_memory_search", {"query": "dogs"})[
                "memories"
            ]
        )
        clients[1]["revoked"] = False
        p.queue_prefetch("dogs")
        assert p.wait_idle()
        assert "dogs private only" in p.prefetch("dogs")
        clients[0]["revoked"] = True
        policy.write_text(json.dumps({"clients": clients}))
        assert p.prefetch("dogs") == ""
        with pytest.raises(PermissionError):
            p.sync_turn("cats denied but retained", "ack")
        assert p.ledger.stats()["events"]["FAILED"] == 1
        clients[0]["revoked"] = False
        policy.write_text(json.dumps({"clients": clients}))
        # Simulate the existing explicit operator retry, then restart with the
        # restored policy. Recovery may save private data, never share the turn.
        p.ledger.retry_failed()
        p.shutdown()
        p = QdrantMemoryProvider(
            plugin_context=SimpleNamespace(llm=LLM()), embedder=Embedder(), overrides=cfg
        )
        p.initialize("next", hermes_home=str(tmp_path))
        assert p.wait_idle()
        private_rows = call(
            p._native_owner.url, tokens["native"], "qdrant_memory_search", {"query": "cats"}
        )["memories"]
        project_rows = call(
            p._native_owner.url, tokens["reader"], "qdrant_memory_search", {"query": "cats"}
        )["memories"]
        assert any(row["text"] == "cats denied but retained" for row in private_rows)
        assert not any(row["text"] == "cats denied but retained" for row in project_rows)
        p.queue_prefetch("cats")
        assert p.wait_idle()
        assert "cats denied but retained" in p.prefetch("cats")

        def failed_write(_):
            raise TimeoutError("synthetic storage acknowledgement failure")

        monkeypatch.setattr(p.store, "upsert", failed_write)
        p.sync_turn("new failing storage fact", "ack")
        assert p.wait_idle()
        assert p._native_owner.owner.blocked is True
        assert p.prefetch("cats") == ""
        assert (
            call(
                p._native_owner.url,
                tokens["native"],
                "qdrant_memory_add",
                {"text": "cannot bypass failed Native operation", "request_id": "blocked"},
            )["error"]["type"]
            == "SharedRecoveryRequiredError"
        )
    finally:
        p.shutdown()


def test_extraction_failure_gates_owner_before_any_operation(tmp_path, monkeypatch):
    """A durable FAILED event cannot be overtaken merely because it has no operations."""
    from .standalone.test_device_grants import grant

    token = "x" * 40
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"clients": [grant(token, "native", owner_id="hermes-user")]}))
    policy.chmod(0o600)
    monkeypatch.setenv("QM_NATIVE_FAILURE", token)

    class FailedLLM(LLM):
        """Fail only the admitted extraction phase."""

        def complete_structured(self, **kwargs):
            raise TimeoutError("synthetic extraction failure")

    cfg = config(
        native_owner={
            "enabled": True,
            "policy": str(policy),
            "port": 0,
            "private_token_env": "QM_NATIVE_FAILURE",
        }
    )
    p = QdrantMemoryProvider(
        plugin_context=SimpleNamespace(llm=FailedLLM()), embedder=Embedder(), overrides=cfg
    )
    p.initialize("first", hermes_home=str(tmp_path))
    try:
        p.sync_turn("cats", "ack")
        assert p.wait_idle()
        assert p.ledger.stats()["events"]["FAILED"] == 1
        assert p._native_owner.owner.blocked is True
        p.sync_turn("later cats", "ack")
        assert p.wait_idle()
        assert p.ledger.stats()["events"]["PENDING"] == 1
        assert p.store.count() == 0
    finally:
        p.shutdown()
