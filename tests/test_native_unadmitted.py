"""Foreign unadmitted input is cancelled without reassigning its identity."""

import argparse
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from qdrant_memory.cli import register_cli, run
from qdrant_memory.ledger import Ledger
from qdrant_memory.native_owner import NativeOwner
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


def test_reauthorization_continues_to_same_scope_input(tmp_path):
    """Cancelling a foreign row does not skip subsequent authorized private input."""
    with closing(Ledger(tmp_path)) as ledger:
        foreign = ledger.enqueue_event(raw_event())
        scope = {"user_id": "owner", "agent_id": None}
        own = ledger.enqueue_event({**raw_event(), "scope": scope})
        admitted = []

        def events(value, *, include_shared):
            assert value["scope"] == scope and include_shared is False
            admitted.append(value)
            return [{**value, "source": "owner_lifecycle", "owner_authority": {}}]

        native = SimpleNamespace(
            owner=SimpleNamespace(runtime=SimpleNamespace(ledger=ledger)),
            scope=SimpleNamespace(as_dict=lambda: scope),
            events=events,
        )
        NativeOwner.reauthorize_pending(native)
        assert ledger.row("events", foreign)["status"] == "CANCELLED"
        assert ledger.row("events", own)["status"] == "PENDING"
        assert json.loads(ledger.row("events", own)["payload_json"])["source"] == "owner_lifecycle"
        assert len(admitted) == 1


@pytest.mark.parametrize("stage", ["candidates", "operation_keys", "owner_authority"])
def test_startup_cannot_discard_prepared_foreign_input(tmp_path, stage):
    """The automatic cancellation path retains the same preparation guards as the CLI."""
    with closing(Ledger(tmp_path)) as ledger:
        key = ledger.enqueue_event({**raw_event(), stage: []})
        before = dict(ledger.row("events", key))
        native = SimpleNamespace(
            owner=SimpleNamespace(runtime=SimpleNamespace(ledger=ledger)),
            scope=SimpleNamespace(as_dict=lambda: {"user_id": "owner", "agent_id": None}),
        )
        with pytest.raises(ValueError):
            NativeOwner.reauthorize_pending(native)
        assert dict(ledger.row("events", key)) == before


@pytest.fixture
def native_provider(tmp_path, monkeypatch):
    """Use a real provider and synthetic scoped credentials for recovery contracts."""
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

    return provider


def test_foreign_unadmitted_retry_can_be_discarded_offline(tmp_path, monkeypatch, native_provider):
    """Confirmed exclusive CLI disposal remains available before restarting the Owner."""
    provider = native_provider
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


def test_foreign_unadmitted_retry_cancels_and_continues_startup(tmp_path, native_provider):
    """Restart never reattributes foreign input or loops on a scope mismatch."""
    p = native_provider()
    p.initialize("first", hermes_home=str(tmp_path))
    cfg = p.cfg
    try:
        p.on_session_switch("first", user_id="other")
        with pytest.raises(PermissionError):
            p.sync_turn("private rejected input", "ack")
    finally:
        p.shutdown()

    from qdrant_memory.config import ledger_namespace

    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    try:
        keys = [row["event_id"] for row in ledger.iter_rows("events", "FAILED")]
        assert keys
        ledger.retry_failed()
    finally:
        ledger.close()
    for session in ("recovered", "restarted"):
        p = native_provider()
        try:
            p.initialize(session, hermes_home=str(tmp_path))
            assert p.store.count() == 0
            for key in keys:
                row = p.ledger.row("events", key)
                assert row["status"] == "CANCELLED"
                assert row["payload_json"] == "{}"
            counter = p.ledger.db.execute(
                "SELECT value FROM counters WHERE name=? AND collection=?",
                ("native_unadmitted_discarded", p.ledger.collection),
            ).fetchone()
            assert counter[0] == len(keys)
        finally:
            p.shutdown()
