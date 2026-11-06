"""Owner lifecycle reuses extraction, durable events, dedupe and the mutation engine."""

import pytest

from qdrant_memory.owner_lifecycle import OwnerLifecycle
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

        def capture_plan(*args):
            plans.append(args[3].copy())
            if len(plans) == 1:
                return "ACCEPTED"
            return run_plan(*args)

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
