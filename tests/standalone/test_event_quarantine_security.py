"""Quarantine stays auditable, bounded and closed to unauthorized actions."""

import json

import pytest

from qdrant_memory.config import ledger_namespace
from qdrant_memory.event_quarantine import extraction_reason
from qdrant_memory.owner_lifecycle import OwnerLifecycle
from qdrant_memory.owner_llm import OwnerExtractionTruncated, OwnerProviderStatus
from qdrant_memory.shared_security import _quarantined, _recovery
from qdrant_memory.shared_service import SharedOwner
from qdrant_memory.shared_state import SharedRecoveryRequiredError, require_no_pending_shared

from ..helpers import config, grant, runtime


def event(identifier="turn-1"):
    """Return one synthetic completed turn with a stable host event ID."""
    return {
        "event_id": identifier,
        "session_id": "session-1",
        "kind": "turn_completed",
        "user": "synthetic preference for the quarantine contract",
        "assistant": "acknowledged",
    }


def configured(tmp_path):
    """Bind an embedded destination path so namespace helpers can resolve it."""
    cfg = config()
    cfg["qdrant"]["path"] = str(tmp_path / "qdrant")
    return cfg


def shared_writer():
    """Return one shared-credential writer and its grant."""
    return grant("opencode-a", visibility="shared", logical="hermes")


def test_reason_codes_are_stable_and_never_copy_provider_text():
    """Classification names the failure class only, with no prompt or body text."""
    assert extraction_reason(OwnerExtractionTruncated("secret prompt text")) == "output_truncated"
    assert extraction_reason(ValueError("secret prompt text")) == "output_invalid"
    assert extraction_reason(TimeoutError("secret host name")) == "timeout"
    assert extraction_reason(ConnectionError("secret host name")) == "provider_unreachable"
    assert extraction_reason(OwnerProviderStatus(401)) == "authentication_rejected"
    assert extraction_reason(OwnerProviderStatus(429)) == "rate_limited"
    assert extraction_reason(OwnerProviderStatus(408)) == "timeout"
    import httpx

    request = httpx.Request("POST", "https://owner.test/v1")
    assert (
        extraction_reason(
            httpx.HTTPStatusError(
                "x", request=request, response=httpx.Response(408, request=request)
            )
        )
        == "timeout"
    )
    assert extraction_reason(OwnerProviderStatus(400)) == "output_invalid"
    assert extraction_reason(OwnerProviderStatus(500)) == "provider_error"


def test_quarantine_retains_raw_input_and_a_bounded_error(tmp_path):
    """The retained row keeps the raw turn and records no model or user text."""
    rt = runtime(tmp_path)
    token, value = shared_writer()
    lifecycle = OwnerLifecycle(SharedOwner(rt, [value]))

    def truncated(_event):
        raise OwnerExtractionTruncated("Owner extraction output was truncated; retain the event")

    rt.extractor.extract = truncated
    try:
        lifecycle.submit(token, event())
        row = rt.ledger.db.execute("SELECT status,payload_json,last_error FROM events").fetchone()
        assert row["status"] == "QUARANTINED"
        plan = json.loads(row["payload_json"])
        assert plan["user"].startswith("synthetic preference")
        assert "candidates" not in plan and "operation_keys" not in plan
        error = json.loads(row["last_error"])
        assert error == {
            "type": "OwnerExtractionTruncated",
            "reason": "output_truncated",
            "retryable": False,
        }
    finally:
        rt.ledger.close()
        rt.store.close()


def test_transient_provider_failure_keeps_the_fail_closed_gate(tmp_path):
    """An exhausted timeout is not a deterministic failure and still blocks writes."""
    rt = runtime(tmp_path)
    token, value = shared_writer()
    owner = SharedOwner(rt, [value])
    lifecycle = OwnerLifecycle(owner)

    def outage(_event):
        raise TimeoutError("synthetic extraction outage")

    rt.extractor.extract = outage
    try:
        with pytest.raises(TimeoutError):
            lifecycle.submit(token, event())
        assert owner.blocked is True
        row = rt.ledger.db.execute("SELECT status FROM events").fetchone()
        assert row["status"] == "FAILED"
        with pytest.raises(SharedRecoveryRequiredError):
            owner.call(token, "qdrant_memory_add", {"text": "synthetic fact", "request_id": "r1"})
    finally:
        rt.ledger.close()
        rt.store.close()


def test_legacy_interlock_allows_maintenance_while_an_event_is_quarantined(tmp_path):
    """The operator CLI can run retry because no write plan is open."""
    rt = runtime(tmp_path)
    token, value = shared_writer()
    lifecycle = OwnerLifecycle(SharedOwner(rt, [value]))

    def truncated(_event):
        raise OwnerExtractionTruncated("Owner extraction output was truncated; retain the event")

    rt.extractor.extract = truncated
    try:
        lifecycle.submit(token, event())
        namespace = rt.ledger.collection
        rt.ledger.close()
        require_no_pending_shared(tmp_path, namespace)
    finally:
        rt.store.close()


def test_doctor_reports_quarantine_without_failing_recovery(tmp_path):
    """Doctor separates retained events from real recovery work."""
    cfg = configured(tmp_path)
    rt = runtime(tmp_path, cfg=cfg, ledger_namespace=ledger_namespace(cfg))
    token, value = shared_writer()
    lifecycle = OwnerLifecycle(SharedOwner(rt, [value]))

    def truncated(_event):
        raise OwnerExtractionTruncated("Owner extraction output was truncated; retain the event")

    rt.extractor.extract = truncated
    try:
        lifecycle.submit(token, event())
    finally:
        rt.ledger.close()
        rt.store.close()

    assert _recovery(tmp_path, cfg) == "PASS"
    assert _quarantined(tmp_path, cfg) == "FAIL"


def test_resubmitting_a_quarantined_event_returns_the_same_status(tmp_path):
    """A repeated host submission must not re-run extraction or duplicate a plan."""
    rt = runtime(tmp_path)
    token, value = shared_writer()
    lifecycle = OwnerLifecycle(SharedOwner(rt, [value]))
    calls = []

    def truncated(_event):
        calls.append(1)
        raise OwnerExtractionTruncated("Owner extraction output was truncated; retain the event")

    rt.extractor.extract = truncated
    try:
        assert lifecycle.submit(token, event())["status"] == "QUARANTINED"
        assert lifecycle.submit(token, event())["status"] == "QUARANTINED"
        assert len(calls) == 1
        assert rt.store.count() == 0
    finally:
        rt.ledger.close()
        rt.store.close()


def test_a_failure_after_extraction_persistence_is_never_quarantined(tmp_path, monkeypatch):
    """A persisted candidate set marks the event FAILED and keeps the gate."""
    rt = runtime(tmp_path)
    token, value = shared_writer()
    owner = SharedOwner(rt, [value])
    lifecycle = OwnerLifecycle(owner)

    def broken_commit(_keys):
        raise RuntimeError("Synthetic commit outage")

    monkeypatch.setattr(rt, "commit", broken_commit)
    try:
        with pytest.raises(RuntimeError):
            lifecycle.submit(token, event())
        row = rt.ledger.db.execute("SELECT status,payload_json FROM events").fetchone()
        assert row["status"] == "FAILED"
        assert "candidates" in json.loads(row["payload_json"])
        assert owner.blocked is True
    finally:
        rt.ledger.close()
        rt.store.close()


def test_recovery_with_a_still_broken_extractor_quarantines_and_returns_zero(tmp_path):
    """A retry that meets the same deterministic failure stays isolated."""
    rt = runtime(tmp_path)
    token, value = shared_writer()
    owner = SharedOwner(rt, [value])
    lifecycle = OwnerLifecycle(owner)

    def truncated(_event):
        raise OwnerExtractionTruncated("Owner extraction output was truncated; retain the event")

    rt.extractor.extract = truncated
    try:
        lifecycle.submit(token, event())
        rt.ledger.retry_failed()
        assert rt.recover() == 0
        assert owner.blocked is False
        assert (
            rt.ledger.db.execute("SELECT status FROM events").fetchone()["status"] == "QUARANTINED"
        )
        added = owner.call(
            token, "qdrant_memory_add", {"text": "synthetic fact", "request_id": "r2"}
        )
        assert added["id"]
    finally:
        rt.ledger.close()
        rt.store.close()


def test_the_ledger_refuses_to_quarantine_a_prepared_row(tmp_path):
    """The defensive invariant holds even when a caller bypasses the runtime."""
    rt = runtime(tmp_path)
    value = {
        "kind": "turn",
        "source": "owner_lifecycle",
        "scope": rt.store.scope_defaults() if hasattr(rt.store, "scope_defaults") else {},
        "session_id": "s",
        "user": "u",
        "assistant": "a",
        "candidates": [],
    }
    key = rt.ledger.enqueue_event(value)
    try:
        with pytest.raises(ValueError):
            rt.ledger.quarantine("events", key, ValueError("x"))
        assert rt.ledger.row("events", key)["status"] == "PENDING"
    finally:
        rt.ledger.close()
        rt.store.close()
