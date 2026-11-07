"""Owner lifecycle reuses extraction, durable events, dedupe and the mutation engine."""

import json
import sqlite3

import pytest

from qdrant_memory.ledger import Ledger
from qdrant_memory.models import digest
from qdrant_memory.owner_lifecycle import (
    OWNER_BACKLOG_PER_AUTHORITY,
    OWNER_BACKLOG_TOTAL,
    OwnerLifecycle,
)
from qdrant_memory.shared_service import SharedOwner

from ..helpers import runtime
from .test_device_grants import grant


@pytest.mark.parametrize("text", ["cats legacy", "cats new"])
def test_lifecycle_never_deduplicates_against_unmarked_legacy(tmp_path, text):
    """Neither exact hash nor semantic relations may promote a legacy private row."""
    token = "a" * 40
    credential = grant(token, "mac", visibility="project", capture_rule_id="approved")
    rt = runtime(tmp_path)
    try:
        owner = SharedOwner(rt, [credential])
        scope = owner.scope(credential)
        legacy = rt.add("cats legacy", scope)
        rt.extractor.llm.relation_value = "SUPERSEDES"
        OwnerLifecycle(owner).submit(
            token,
            {
                "event_id": "isolated",
                "session_id": "s",
                "kind": "turn_completed",
                "user": text,
                "assistant": "ack",
            },
        )
        rows = owner.call(token, "qdrant_memory_search", {"query": "cats"})["memories"]
        assert len(rows) == 1
        assert rows[0]["text"] == text
        original = rt.store.get(legacy["id"], scope)
        assert original.payload["text"] == "cats legacy"
        assert "shared_protocol" not in original.payload
        assert len(rt.extractor.llm.calls) == 1
    finally:
        rt.ledger.close()
        rt.store.close()


def test_private_lifecycle_idempotency_provenance_and_shared_rule(tmp_path):
    """A completed permitted turn is extracted centrally and returned through Owner tools."""
    token = "a" * 40
    credential = grant(token, "mac")
    rt = runtime(tmp_path)
    try:
        owner = SharedOwner(rt, [credential])
        lifecycle = OwnerLifecycle(owner)
        event = {
            "event_id": "turn-one",
            "session_id": "session",
            "kind": "turn_completed",
            "user": "cats private",
            "assistant": "ack",
        }
        result = lifecycle.submit(token, event)
        assert result["status"] == "COMMITTED"
        assert lifecycle.submit(token, event) == result
        assert len(rt.extractor.llm.calls) == 1
        rows = owner.call(token, "qdrant_memory_search", {"query": "cats"})["memories"]
        assert len(rows) == 1
        assert rows[0]["created_by_device"] == "mac"
        with pytest.raises(ValueError):
            lifecycle.submit(token, {**event, "user": "other"})
        with pytest.raises(ValueError):
            lifecycle.submit(token, {**event, "principal": "spoof"})
        credential["visibility"] = "project"
        with pytest.raises(PermissionError):
            lifecycle.submit(token, {**event, "event_id": "project"})
        credential["capture_rule_id"] = "user-approved-alpha"
        assert lifecycle.submit(token, {**event, "event_id": "project"})["status"] == "COMMITTED"
    finally:
        rt.ledger.close()
        rt.store.close()


def test_preextracted_turn_uses_runtime_writer_and_replays_without_reextracting(
    tmp_path, monkeypatch
):
    """Owner retries its stored candidate plan without re-extracting or returning content."""
    token = "a" * 40
    rt = runtime(tmp_path)
    try:
        owner = SharedOwner(rt, [grant(token, "mac")])
        lifecycle = OwnerLifecycle(owner)
        event = {
            "event_id": "dsh-turn-one",
            "session_id": "hashed-session-id",
            "kind": "turn_completed",
            "candidate_schema": "qdrant-memory/extraction-v1",
            "candidates": [
                {
                    "text": "The DSH native adapter uses the existing Runtime writer.",
                    "category": ["project"],
                    "importance": 0.8,
                    "replacement_intent": False,
                }
            ],
        }
        plans = []
        run_plan = lifecycle._run_plan

        def capture_plan(*args, **kwargs):
            plans.append(args[3].copy())
            if len(plans) == 1:
                return "ACCEPTED"
            return run_plan(*args, **kwargs)

        monkeypatch.setattr(lifecycle, "_run_plan", capture_plan)
        accepted = lifecycle.submit(token, event)
        assert accepted["status"] == "ACCEPTED"
        assert lifecycle.inspect(token, event["event_id"])["status"] == "ACCEPTED"
        result = lifecycle.retry(token, event["event_id"])
        assert result["status"] == "COMMITTED"
        assert lifecycle.submit(token, event) == result
        assert len(plans) == 2
        assert plans[0] == plans[1]
        assert "user" not in plans[0] and "assistant" not in plans[0]
        assert rt.extractor.llm.calls == []
        rows = owner.call(token, "qdrant_memory_search", {"query": "DSH native adapter"})[
            "memories"
        ]
        assert len(rows) == 1
        assert rows[0]["text"] == event["candidates"][0]["text"]
        assert rows[0]["created_by_device"] == "mac"
    finally:
        rt.ledger.close()
        rt.store.close()


def test_preextracted_replacement_keeps_only_exact_user_evidence(tmp_path):
    """A minimal exact excerpt lets Runtime review corrections without assistant text."""
    import json
    from types import SimpleNamespace

    token = "a" * 40
    statement = "For Project Willow, replace the morning cat preference with evening."

    class ReplacementLLM:
        def __init__(self):
            self.calls = []

        def complete_structured(self, **kwargs):
            self.calls.append(kwargs)
            value = json.loads(kwargs["input"][0]["text"])
            relation = "SUPERSEDES" if "user_statement" in value else "CONFLICT"
            return SimpleNamespace(parsed={"relation": relation})

    llm = ReplacementLLM()
    rt = runtime(tmp_path, llm=llm)
    try:
        credential = grant(token, "mac")
        owner = SharedOwner(rt, [credential])
        scope = owner.scope(credential)
        old = owner.call(
            token,
            "qdrant_memory_add",
            {"text": "Project Willow prefers cats in the morning", "request_id": "seed-old"},
        )
        lifecycle = OwnerLifecycle(owner)
        event = {
            "event_id": "dsh-replacement",
            "session_id": "hashed-session-id",
            "kind": "turn_completed",
            "candidate_schema": "qdrant-memory/extraction-v1",
            "replacement_context": statement,
            "candidates": [
                {
                    "text": "Project Willow prefers cats in the evening",
                    "category": ["preference"],
                    "importance": 0.8,
                    "replacement_intent": True,
                }
            ],
        }
        assert lifecycle.submit(token, event)["status"] == "COMMITTED"
        updated = rt.store.get(old["id"], scope)
        assert updated.payload["text"] == "Project Willow prefers cats in the evening"
        relation_inputs = [
            json.loads(call["input"][0]["text"])
            for call in llm.calls
            if call["purpose"].endswith("relation")
        ]
        assert any(value.get("user_statement") == statement for value in relation_inputs)
        assert all("assistant" not in call for call in relation_inputs)
    finally:
        rt.ledger.close()
        rt.store.close()


@pytest.mark.parametrize(
    "change",
    [
        {"candidate_schema": "qdrant-memory/extraction-v2"},
        {"user": "raw transcript is not part of the candidate contract"},
        {"replacement_context": "No candidate marked replacement."},
        {
            "candidates": [
                {
                    "text": "A corrected fact",
                    "category": [],
                    "importance": 0.5,
                    "replacement_intent": True,
                }
            ]
        },
        {
            "candidates": [
                {
                    "text": "A fact",
                    "category": [],
                    "importance": True,
                    "replacement_intent": False,
                }
            ]
        },
        {
            "candidates": [
                {
                    "text": "A fact",
                    "category": [],
                    "importance": 0.5,
                    "replacement_intent": False,
                    "scope": {"owner_id": "spoofed"},
                }
            ]
        },
    ],
)
def test_preextracted_turn_rejects_unknown_schema_or_fields(tmp_path, change):
    """Candidate ingress is versioned, bounded, and cannot widen Owner authority."""
    token = "a" * 40
    rt = runtime(tmp_path)
    try:
        lifecycle = OwnerLifecycle(SharedOwner(rt, [grant(token, "mac")]))
        event = {
            "event_id": "dsh-invalid",
            "session_id": "s",
            "kind": "turn_completed",
            "candidate_schema": "qdrant-memory/extraction-v1",
            "candidates": [
                {
                    "text": "A fact",
                    "category": [],
                    "importance": 0.5,
                    "replacement_intent": False,
                }
            ],
        }
        with pytest.raises(ValueError, match="Invalid lifecycle event"):
            lifecycle.submit(token, {**event, **change})
        assert rt.ledger.stats()["events"].get("PENDING", 0) == 0
    finally:
        rt.ledger.close()
        rt.store.close()


def test_lifecycle_outage_is_durable_and_recoverable(tmp_path, monkeypatch):
    """The accepted event survives extraction failure without a second runtime."""
    token = "a" * 40
    rt = runtime(tmp_path)
    try:
        owner = SharedOwner(rt, [grant(token, "mac")])
        lifecycle = OwnerLifecycle(owner)
        extract = rt.extractor.extract

        def fail(_):
            raise TimeoutError("synthetic-private-error")

        monkeypatch.setattr(rt.extractor, "extract", fail)
        event = {
            "event_id": "outage",
            "session_id": "s",
            "kind": "turn_completed",
            "user": "cats durable",
            "assistant": "ack",
        }
        with pytest.raises(TimeoutError):
            lifecycle.submit(token, event)
        assert rt.ledger.stats()["events"]["FAILED"] == 1
        assert owner.blocked is True
        monkeypatch.setattr(rt.extractor, "extract", extract)
        rt.ledger.retry_failed()
        owner = SharedOwner(rt, [grant(token, "mac")])
        lifecycle = OwnerLifecycle(owner)
        assert lifecycle.recover() == 0
        assert lifecycle.submit(token, event)["status"] == "COMMITTED"
        assert len(owner.call(token, "qdrant_memory_search", {"query": "cats"})["memories"]) == 1
    finally:
        rt.ledger.close()
        rt.store.close()


def test_backlog_budget_is_partitioned_by_authority(tmp_path):
    """One capture principal cannot spend another principal's backlog budget."""
    token_a, token_b = "a" * 40, "b" * 40
    grant_a = grant(token_a, "mac", visibility="project", capture_rule_id="approved")
    grant_b = grant(token_b, "linux", visibility="project", capture_rule_id="approved")
    rt = runtime(tmp_path)
    try:
        owner = SharedOwner(rt, [grant_a, grant_b])
        lifecycle = OwnerLifecycle(owner)
        authority_a = lifecycle._authority(token_a)
        authority_b = lifecycle._authority(token_b)
        assert authority_a != authority_b
        for index in range(128):
            rt.ledger.db.execute(
                "INSERT INTO events(event_id,session_id,payload_json,status,collection,owner_authority) "
                "VALUES(?,?,?,?,?,?)",
                (
                    f"quarantined-{index}",
                    "session",
                    "{}",
                    "QUARANTINED",
                    rt.ledger.collection,
                    digest(authority_a),
                ),
            )
        rt.ledger.db.commit()
        event = {
            "event_id": "turn",
            "session_id": "session",
            "kind": "turn_completed",
            "user": "cats",
            "assistant": "ack",
        }
        value, _ = lifecycle._new_plan(rt.ledger, "request-b", "hash", authority_b, event)
        assert value["owner_authority"] == authority_b
        with pytest.raises(RuntimeError, match="backlog is full"):
            lifecycle._new_plan(rt.ledger, "request-a", "hash", authority_a, event)
    finally:
        rt.ledger.close()
        rt.store.close()


def test_ledger_enforces_backlog_for_direct_native_admission(tmp_path):
    """Native admission goes through enqueue_event and must hit the same budget."""
    rt = runtime(tmp_path)
    authority = {
        "principal": "mac",
        "owner_id": "alice",
        "space_id": "hermes-private",
        "visibility": "project",
        "logical_agent_id": "hermes",
        "device_id": "mac",
        "capture_rule_id": "approved",
    }
    try:
        for index in range(128):
            rt.ledger.db.execute(
                "INSERT INTO events(event_id,session_id,payload_json,status,collection,owner_authority) "
                "VALUES(?,?,?,?,?,?)",
                (
                    f"native-{index}",
                    "session",
                    "{}",
                    "QUARANTINED",
                    rt.ledger.collection,
                    digest(authority),
                ),
            )
        rt.ledger.db.commit()
        with pytest.raises(RuntimeError, match="backlog is full"):
            rt.ledger.enqueue_event({"session_id": "session", "owner_authority": authority})
        other = {**authority, "principal": "linux", "device_id": "linux"}
        rt.ledger.enqueue_event({"session_id": "session", "owner_authority": other})
    finally:
        rt.ledger.close()
        rt.store.close()


def test_collection_backlog_ceiling_bounds_all_authorities(tmp_path):
    """A collection-wide ceiling still bounds the sum of per-authority budgets."""
    token = "a" * 40
    credential = grant(token, "mac", visibility="project", capture_rule_id="approved")
    rt = runtime(tmp_path)
    try:
        owner = SharedOwner(rt, [credential])
        lifecycle = OwnerLifecycle(owner)
        for index in range(OWNER_BACKLOG_TOTAL):
            rt.ledger.db.execute(
                "INSERT INTO events(event_id,session_id,payload_json,status,collection,owner_authority) "
                "VALUES(?,?,?,?,?,?)",
                (
                    f"other-{index}",
                    "session",
                    "{}",
                    "PENDING",
                    rt.ledger.collection,
                    digest({"principal": "other"}),
                ),
            )
        rt.ledger.db.commit()
        event = {
            "event_id": "turn",
            "session_id": "session",
            "kind": "turn_completed",
            "user": "cats",
            "assistant": "ack",
        }
        with pytest.raises(RuntimeError, match="backlog is full"):
            lifecycle._new_plan(rt.ledger, "request-a", "hash", lifecycle._authority(token), event)
    finally:
        rt.ledger.close()
        rt.store.close()


def test_unattributed_native_rejections_do_not_consume_other_authority_budget(tmp_path):
    """Retained Native rejections stay recoverable without charging a grant."""
    rt = runtime(tmp_path)
    authority = {
        "principal": "mac",
        "owner_id": "alice",
        "space_id": "hermes-private",
        "visibility": "project",
        "logical_agent_id": "hermes",
        "device_id": "mac",
        "capture_rule_id": "approved",
    }
    try:
        for index in range(OWNER_BACKLOG_TOTAL):
            rt.ledger.db.execute(
                "INSERT INTO events(event_id,session_id,payload_json,status,collection,owner_authority) "
                "VALUES(?,?,?,?,?,?)",
                (f"unadmitted-{index}", "session", "{}", "FAILED", rt.ledger.collection, ""),
            )
        rt.ledger.db.commit()
        assert rt.ledger.backlog_counts(digest(authority))[1] == 0
        rt.ledger.enqueue_event({"session_id": "session", "owner_authority": authority})
        assert rt.ledger.backlog_counts(digest(authority))[0] == 1
        rejected = rt.ledger.enqueue_event(
            {"session_id": "session", "source": "native_owner_unadmitted"}
        )
        row = rt.ledger.row("events", rejected)
        assert row is not None and row["status"] == "PENDING"
    finally:
        rt.ledger.close()
        rt.store.close()


def test_recover_retries_deferred_receipts_after_capacity_is_freed(tmp_path, monkeypatch):
    """A receipt refused for a full backlog is retried once recovery frees space."""
    token = "a" * 40
    credential = grant(token, "mac", visibility="project", capture_rule_id="approved")
    rt = runtime(tmp_path)
    try:
        owner = SharedOwner(rt, [credential])
        lifecycle = OwnerLifecycle(owner)
        authority = lifecycle._authority(token)
        for index in range(OWNER_BACKLOG_PER_AUTHORITY):
            rt.ledger.db.execute(
                "INSERT INTO events(event_id,session_id,payload_json,status,collection,owner_authority) "
                "VALUES(?,?,?,?,?,?)",
                (
                    f"queued-{index}",
                    "session",
                    "{}",
                    "PENDING",
                    rt.ledger.collection,
                    digest(authority),
                ),
            )
        deferred_value = {"session_id": "session", "owner_authority": authority}
        deferred_key = digest([rt.ledger.collection, deferred_value])
        rt.ledger.db.execute(
            "INSERT INTO owner_events_v1 VALUES(?,?,?,?,?)",
            (
                rt.ledger.collection,
                "receipt",
                "hash",
                deferred_key,
                json.dumps(deferred_value),
            ),
        )
        rt.ledger.db.commit()

        processed = []
        monkeypatch.setattr(rt, "process_event", processed.append)
        recoveries = []

        def recover():
            if not recoveries:
                rt.ledger.db.execute(
                    "UPDATE events SET status='COMMITTED' WHERE event_id LIKE 'queued-%'"
                )
                rt.ledger.db.commit()
            recoveries.append(1)
            return 0

        monkeypatch.setattr(rt, "recover", recover)
        assert lifecycle.recover() == 0
        assert len(recoveries) == 1
        assert rt.ledger.row("events", deferred_key) is not None
        assert deferred_key in processed
    finally:
        rt.ledger.close()
        rt.store.close()


def test_recover_keeps_a_still_full_receipt_without_spinning(tmp_path, monkeypatch):
    """A still-full budget leaves the receipt for the next start, not a retry loop."""
    token = "a" * 40
    credential = grant(token, "mac", visibility="project", capture_rule_id="approved")
    rt = runtime(tmp_path)
    try:
        owner = SharedOwner(rt, [credential])
        lifecycle = OwnerLifecycle(owner)
        authority = lifecycle._authority(token)
        for index in range(OWNER_BACKLOG_PER_AUTHORITY):
            rt.ledger.db.execute(
                "INSERT INTO events(event_id,session_id,payload_json,status,collection,owner_authority) "
                "VALUES(?,?,?,?,?,?)",
                (
                    f"queued-{index}",
                    "session",
                    "{}",
                    "PENDING",
                    rt.ledger.collection,
                    digest(authority),
                ),
            )
        deferred_value = {"session_id": "session", "owner_authority": authority}
        deferred_key = digest([rt.ledger.collection, deferred_value])
        rt.ledger.db.execute(
            "INSERT INTO owner_events_v1 VALUES(?,?,?,?,?)",
            (
                rt.ledger.collection,
                "receipt",
                "hash",
                deferred_key,
                json.dumps(deferred_value),
            ),
        )
        rt.ledger.db.commit()
        recoveries = []
        monkeypatch.setattr(rt, "recover", lambda: recoveries.append(1) or 0)
        assert lifecycle.recover() == 0
        assert len(recoveries) == 1
        assert rt.ledger.row("events", deferred_key) is None
        receipt = rt.ledger.db.execute(
            "SELECT event_json FROM owner_events_v1 WHERE collection=? AND request_key='receipt'",
            (rt.ledger.collection,),
        ).fetchone()
        assert receipt is not None and receipt["event_json"] != "{}"
    finally:
        rt.ledger.close()
        rt.store.close()


def test_backlog_refusal_keeps_an_existing_receipt_and_drops_a_new_one(tmp_path, monkeypatch):
    """Only a freshly created plan may lose its receipt on a budget refusal."""
    rt = runtime(tmp_path)
    try:
        ledger = rt.ledger
        ledger.db.execute(
            "CREATE TABLE IF NOT EXISTS owner_events_v1 (collection TEXT NOT NULL, "
            "request_key TEXT NOT NULL, input_hash TEXT NOT NULL, event_key TEXT NOT NULL, "
            "event_json TEXT NOT NULL, PRIMARY KEY(collection, request_key))"
        )
        for request_key in ("existing", "fresh"):
            ledger.db.execute(
                "INSERT INTO owner_events_v1 VALUES(?,?,?,?,?)",
                (ledger.collection, request_key, "hash", "event", "{}"),
            )
        ledger.db.commit()

        def refuse(_value):
            raise RuntimeError("Owner lifecycle backlog is full")

        monkeypatch.setattr(ledger, "enqueue_event", refuse)
        with pytest.raises(RuntimeError, match="backlog is full"):
            OwnerLifecycle._run_plan(None, None, ledger, "existing", {}, "event", created=False)
        kept = ledger.db.execute(
            "SELECT 1 FROM owner_events_v1 WHERE collection=? AND request_key='existing'",
            (ledger.collection,),
        ).fetchone()
        assert kept is not None
        with pytest.raises(RuntimeError, match="backlog is full"):
            OwnerLifecycle._run_plan(None, None, ledger, "fresh", {}, "event", created=True)
        dropped = ledger.db.execute(
            "SELECT 1 FROM owner_events_v1 WHERE collection=? AND request_key='fresh'",
            (ledger.collection,),
        ).fetchone()
        assert dropped is None
    finally:
        rt.ledger.close()
        rt.store.close()


def test_ledger_adds_owner_authority_to_an_older_events_table(tmp_path):
    """An existing profile keeps its events and gains the partition column."""
    home = tmp_path / "profile"
    directory = home / "qdrant-memory"
    directory.mkdir(parents=True)
    database = sqlite3.connect(directory / "state.db")
    database.execute(
        "CREATE TABLE events (event_id TEXT PRIMARY KEY, session_id TEXT, "
        "payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', "
        "attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at TEXT, "
        "committed_at TEXT, collection TEXT NOT NULL)"
    )
    database.execute(
        "INSERT INTO events(event_id,session_id,payload_json,collection) "
        "VALUES('old','session','{}','hermes_qdrant_memory')"
    )
    authority = {
        "principal": "mac",
        "owner_id": "alice",
        "space_id": "hermes-private",
        "visibility": "project",
        "logical_agent_id": "hermes",
        "device_id": "mac",
        "capture_rule_id": "approved",
    }
    database.execute(
        "INSERT INTO events(event_id,session_id,payload_json,collection) VALUES(?,?,?,?)",
        (
            "old-lifecycle",
            "session",
            json.dumps({"owner_authority": authority}),
            "hermes_qdrant_memory",
        ),
    )
    database.execute(
        "INSERT INTO events(event_id,session_id,payload_json,collection) VALUES(?,?,?,?)",
        (
            "other-collection",
            "session",
            json.dumps({"owner_authority": authority}),
            "other_collection",
        ),
    )
    for event_id, status, payload in (
        ("failed-lifecycle", "FAILED", json.dumps({"owner_authority": authority})),
        ("quarantined-lifecycle", "QUARANTINED", json.dumps({"owner_authority": authority})),
        ("invalid-json", "FAILED", "{not-json"),
        ("non-dict-authority", "QUARANTINED", json.dumps({"owner_authority": "scalar"})),
    ):
        database.execute(
            "INSERT INTO events(event_id,session_id,payload_json,status,collection) "
            "VALUES(?,?,?,?,?)",
            (event_id, "session", payload, status, "hermes_qdrant_memory"),
        )
    database.commit()
    database.close()
    ledger = Ledger(home)
    try:
        columns = {row[1] for row in ledger.db.execute("PRAGMA table_info(events)")}
        assert "owner_authority" in columns
        row = ledger.db.execute(
            "SELECT owner_authority FROM events WHERE event_id='old'"
        ).fetchone()
        assert row[0] == ""
        migrated = ledger.db.execute(
            "SELECT owner_authority FROM events WHERE event_id='old-lifecycle'"
        ).fetchone()
        assert migrated[0] == digest(authority)
        other = ledger.db.execute(
            "SELECT owner_authority FROM events WHERE event_id='other-collection'"
        ).fetchone()
        assert other[0] == ""
        for event_id in ("failed-lifecycle", "quarantined-lifecycle"):
            row = ledger.db.execute(
                "SELECT owner_authority,status,payload_json FROM events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            assert row["owner_authority"] == digest(authority)
            assert row["status"] in {"FAILED", "QUARANTINED"}
            assert row["payload_json"] != "{}"
        for event_id in ("invalid-json", "non-dict-authority"):
            row = ledger.db.execute(
                "SELECT owner_authority,payload_json FROM events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            assert row["owner_authority"] == ""
            assert row["payload_json"] != "{}"
        marker = ledger.db.execute(
            "SELECT completed_at FROM ledger_backfills WHERE collection=?",
            (ledger.collection,),
        ).fetchone()
        assert marker is not None and marker["completed_at"]
    finally:
        ledger.close()

    reopened = Ledger(home)
    try:
        assert reopened.db.execute(
            "SELECT owner_authority FROM events WHERE event_id='old-lifecycle'"
        ).fetchone()[0] == digest(authority)
        assert (
            reopened.db.execute(
                "SELECT owner_authority FROM events WHERE event_id='invalid-json'"
            ).fetchone()[0]
            == ""
        )
        assert (
            reopened.db.execute(
                "SELECT payload_json FROM events WHERE event_id='non-dict-authority'"
            ).fetchone()[0]
            != "{}"
        )
    finally:
        reopened.close()
