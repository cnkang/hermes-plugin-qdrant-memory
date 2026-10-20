"""Unadmitted input requires explicit offline disposal, never identity reassignment."""

import argparse
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from qdrant_memory.cli import register_cli, run
from qdrant_memory.ledger import Ledger
from qdrant_memory.provider import QdrantMemoryProvider

from .helpers import LLM, Embedder, config
from .standalone.test_device_grants import grant


def raw_event(source="native_owner_unadmitted"):
    """Build synthetic input that never received an Owner grant."""
    return {
        "kind": "turn",
        "scope": {"user_id": "other", "agent_id": None},
        "source": source,
        "session_id": "s",
        "user": "private raw input",
        "assistant": "ack",
    }


@pytest.mark.parametrize(
    "source,stage",
    [("owner_lifecycle", None)]
    + [
        ("native_owner_unadmitted", field)
        for field in ("candidates", "operation_keys", "owner_authority")
    ],
)
def test_discard_refuses_admitted_or_prepared_events(tmp_path, source, stage):
    """Accepted authority or durable preparation always requires recovery."""
    ledger = Ledger(tmp_path)
    try:
        event = raw_event(source)
        key = ledger.enqueue_event(event)
        if stage:
            ledger.prepare_event(key, {**event, stage: []})
        before = dict(ledger.row("events", key))
        with pytest.raises(ValueError):
            ledger.discard_unadmitted_event(key)
        assert dict(ledger.row("events", key)) == before
    finally:
        ledger.close()


def test_cancelled_event_payload_is_scrubbed(tmp_path):
    """Both direct completion and startup cleanup remove cancelled raw bodies."""
    ledger = Ledger(tmp_path)
    try:
        key = ledger.enqueue_event(raw_event())
        ledger.finish("events", key, "CANCELLED")
        assert ledger.row("events", key)["payload_json"] == "{}"
        # Model a terminal row retained by a previous version's cleanup query.
        with ledger.db:
            ledger.db.execute(
                "UPDATE events SET payload_json=? WHERE event_id=?", (json.dumps(raw_event()), key)
            )
    finally:
        ledger.close()
    ledger = Ledger(tmp_path)
    try:
        assert ledger.row("events", key)["payload_json"] == "{}"
    finally:
        ledger.close()


def test_discard_and_counter_commit_atomically(tmp_path):
    """A counter write failure rolls back disposal, preserving retryable raw input."""
    ledger = Ledger(tmp_path)
    try:
        key = ledger.enqueue_event(raw_event())
        with ledger.db:
            ledger.db.execute(
                "CREATE TRIGGER reject_discard_counter BEFORE INSERT ON counters "
                "WHEN NEW.name='native_unadmitted_discarded' "
                "BEGIN SELECT RAISE(ABORT,'counter unavailable'); END"
            )
        with pytest.raises(sqlite3.IntegrityError):
            ledger.discard_unadmitted_event(key)
        assert ledger.row("events", key)["status"] == "PENDING"
        assert ledger.row("events", key)["payload_json"] != "{}"
    finally:
        ledger.close()


def test_discard_cannot_target_another_destination(tmp_path):
    """A ledger namespace cannot dispose of another destination's input."""
    first, other = Ledger(tmp_path, "first"), Ledger(tmp_path, "other")
    try:
        key = first.enqueue_event(raw_event())
        with pytest.raises(ValueError):
            other.discard_unadmitted_event(key)
        assert first.row("events", key)["status"] == "PENDING"
        assert first.row("events", key)["payload_json"] != "{}"
    finally:
        first.close()
        other.close()


def test_foreign_unadmitted_retry_requires_explicit_offline_discard(tmp_path, monkeypatch):
    """Real startup preserves foreign raw input until a confirmed, exclusive CLI action."""
    token = "n" * 40
    monkeypatch.setenv("QM_NATIVE_SCOPE", token)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"clients": [grant(token, "native", owner_id="hermes-user")]}))
    policy.chmod(0o600)
    cfg = config(
        native_owner={
            "enabled": True,
            "policy": str(policy),
            "port": 0,
            "private_token_env": "QM_NATIVE_SCOPE",
        }
    )

    def provider():
        return QdrantMemoryProvider(
            plugin_context=SimpleNamespace(llm=LLM()), embedder=Embedder(), overrides=cfg
        )

    p = provider()
    p.initialize("first", hermes_home=str(tmp_path))
    resolved_cfg = p.cfg
    try:
        p.on_session_switch("first", user_id="other")
        with pytest.raises(PermissionError):
            p.sync_turn("private rejected input", "ack")
    finally:
        p.shutdown()
    from qdrant_memory.config import ledger_namespace

    ledger = Ledger(tmp_path, ledger_namespace(resolved_cfg))
    try:
        keys = [row["event_id"] for row in ledger.iter_rows("events", "FAILED")]
        assert keys
        ledger.retry_failed()
    finally:
        ledger.close()
    p = provider()
    try:
        with pytest.raises(PermissionError, match="another logical identity") as error:
            p.initialize("blocked", hermes_home=str(tmp_path))
        assert any(key in str(error.value) for key in keys)
    finally:
        p.shutdown()

    parser = argparse.ArgumentParser()
    register_cli(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["discard-unadmitted-event", "--event-id", keys[0]])

    def loader(*_args, **kwargs):
        assert kwargs["resolve_secrets"] is False
        return resolved_cfg

    monkeypatch.setattr(
        "qdrant_memory.cli.build_embedder",
        lambda *_a, **_k: pytest.fail("offline command opened embeddings"),
    )
    for key in keys:
        args = parser.parse_args(
            ["discard-unadmitted-event", "--event-id", key, "--confirm-discard"]
        )
        from qdrant_memory.ownership import WriterBusyError, WriterLease

        with closing(WriterLease.for_config(tmp_path, resolved_cfg)):
            with pytest.raises(WriterBusyError):
                run(args, home=tmp_path, config_loader=loader)
        result = run(args, home=tmp_path, config_loader=loader)
        assert result == {"event_id": key, "status": "CANCELLED", "discarded": True}
        assert run(args, home=tmp_path, config_loader=loader)["discarded"] is False
    p = provider()
    p.initialize("recovered", hermes_home=str(tmp_path))
    try:
        assert p.store.count() == 0
        for key in keys:
            row = p.ledger.row("events", key)
            assert row["status"] == "CANCELLED" and row["payload_json"] == "{}"
    finally:
        p.shutdown()
