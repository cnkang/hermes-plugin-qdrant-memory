"""Hard invariants of a soak window, exercised on synthetic evidence."""

import copy

from qdrant_memory.models import content_hash
from scripts.soak.common import Invariant, overall_status
from scripts.soak.prepare import build_grants
from scripts.soak.verify import (
    REQUIRED_INVARIANTS,
    Evidence,
    as_dicts,
    event_receipt_key,
    missing_invariants,
    request_receipt_key,
    verify,
)

RECORD_ID = "11111111-1111-5111-8111-111111111111"
DELETED_ID = "22222222-2222-5222-8222-222222222222"
EVENT_ID = "w0-event-1"
TEXT_V1 = "Fact SOAK-w0-1-ab12cd34: the ledger path keeps the write-ahead journal."
TEXT_V2 = "Fact SOAK-w0-1-ab12cd34: the ledger path keeps the write-ahead journal. Revision 9."
TEXT_V3 = TEXT_V2 + " Revision 20 keeps the same subject."
EVENT_TEXT = "Fact SOAK-w0-2-ef56ab78-turn: the lifecycle path captures one user turn durably."


def _journal_rows():
    """Return one complete, healthy evidence stream."""
    return [
        {"kind": "run_start", "run_id": "soak-quick-steady-1"},
        {"kind": "attempt", "op": "add", "request_id": "w0-add-1"},
        {
            "kind": "op",
            "op": "add",
            "tool": "add",
            "identity": "hermes-a",
            "request_id": "w0-add-1",
            "record_id": RECORD_ID,
            "version": "v1",
            "action": "ADD",
            "result": "ok",
            "text": TEXT_V1,
            "args": {"text": TEXT_V1},
            "duration_ms": 12.0,
        },
        {
            "kind": "op",
            "op": "update",
            "tool": "update",
            "identity": "hermes-b",
            "request_id": "w0-update-9",
            "record_id": RECORD_ID,
            "version": "v2",
            "action": "UPDATE",
            "result": "ok",
            "text": TEXT_V2,
            "args": {"id": RECORD_ID, "text": TEXT_V2, "expected_version": "v1"},
            "duration_ms": 14.0,
        },
        {
            "kind": "op",
            "op": "add",
            "tool": "add",
            "identity": "hermes-b",
            "request_id": "w0-disposable-3",
            "record_id": DELETED_ID,
            "version": "d1",
            "action": "ADD",
            "result": "ok",
            "text": "Fact SOAK-w0-3-zz99: the disposable path is removed by the delete path.",
            "args": {"text": "Fact SOAK-w0-3-zz99"},
            "duration_ms": 9.0,
        },
        {
            "kind": "op",
            "op": "delete",
            "tool": "delete",
            "identity": "codex",
            "request_id": "w0-delete-4",
            "record_id": DELETED_ID,
            "version": "d1",
            "action": "DELETE",
            "result": "ok",
            "text": "Fact SOAK-w0-3-zz99",
            "args": {"id": DELETED_ID, "expected_version": "d1"},
            "duration_ms": 8.0,
        },
        {
            "kind": "op",
            "op": "event",
            "tool": "event",
            "identity": "hermes-a",
            "event_id": EVENT_ID,
            "status": "COMMITTED",
            "result": "ok",
            "event_text": EVENT_TEXT,
            "duration_ms": 30.0,
        },
        {
            "kind": "retrieval",
            "identity": "codex",
            "marker": "SOAK-w0-1-ab12cd34",
            "record_id": RECORD_ID,
            "hit": True,
        },
        {
            "kind": "probe",
            "probe": "authorization",
            "status": "PASS",
            "marker": "SOAK-probe-1",
            "private_write": True,
            "readonly_denied": True,
            "cross_scope_hidden": True,
            "private_record": "33333333-3333-5333-8333-333333333333",
            "text": "Fact SOAK-probe-private: the private partition keeps its own rows.",
        },
        {"kind": "metric", "rss_bytes": 1000.0, "file_descriptors": 11},
        {"kind": "metric", "rss_bytes": 1200.0, "file_descriptors": 12},
        {"kind": "metric", "rss_bytes": 1400.0, "file_descriptors": 12},
    ]


def _ledger(grants):
    """Return a healthy ledger view for the same window."""
    by_principal = {grant["principal"]: grant for grant in grants}
    rows = []
    for identity, request_id in (
        ("hermes-a", "w0-add-1"),
        ("hermes-b", "w0-update-9"),
        ("hermes-b", "w0-disposable-3"),
        ("codex", "w0-delete-4"),
    ):
        rows.append(
            {
                "request_key": request_receipt_key(by_principal[identity], request_id),
                "done": 1,
                "plan": "null",
                "input_hash": "x",
                "principal": identity,
            }
        )
    return {
        "available": True,
        "integrity": "ok",
        "operations": [{"key": "op1", "status": "COMMITTED"}],
        "events": [{"key": EVENT_ID, "status": "COMMITTED"}],
        "requests": rows,
        "owner_events": [
            {
                "request_key": event_receipt_key(by_principal["hermes-a"], EVENT_ID),
                "event_key": EVENT_ID,
                "event_json": "{}",
            }
        ],
    }


def _points(grants):
    """Return the exact points the healthy window must have produced."""
    private_grant = next(grant for grant in grants if grant["principal"] == "hermes-private")
    return [
        {
            "id": RECORD_ID,
            "payload": {
                "text": TEXT_V2,
                "version": "v2",
                "content_hash": content_hash(TEXT_V2),
                "space_id": "alpha",
                "visibility": "project",
            },
        },
        {
            "id": "44444444-4444-5444-8444-444444444444",
            "payload": {
                "text": EVENT_TEXT,
                "version": "e1",
                "content_hash": content_hash(EVENT_TEXT),
            },
        },
        {
            "id": "33333333-3333-5333-8333-333333333333",
            "payload": {
                "text": "Fact SOAK-probe-private: the private partition keeps its own rows.",
                "version": "p1",
                "content_hash": content_hash(
                    "Fact SOAK-probe-private: the private partition keeps its own rows."
                ),
                "space_id": private_grant["space_id"],
                "visibility": "private",
            },
        },
    ]


def _verify(rows, evidence, **overrides):
    """Verify the synthetic window with production defaults."""
    grants, _ = build_grants()
    options = {
        "grants": grants,
        "identity": None,
        "duration_seconds": 480.0,
        "target_seconds": 480,
        "owner_restarts": 3,
        "owner_starts": 4,
        "start_failures": 0,
    }
    options.update(overrides)
    return verify(rows, evidence, **options)


def _healthy():
    """Return the healthy window's rows and evidence."""
    grants, _ = build_grants()
    evidence = Evidence(points=_points(grants), ledger=_ledger(grants), integrity="ok")
    return _journal_rows(), evidence


def _status(invariants, name):
    """Return one invariant's status."""
    return next(item.status for item in invariants if item.name == name)


def test_healthy_window_passes_every_required_invariant():
    """The synthetic healthy window must satisfy the full invariant set."""
    rows, evidence = _healthy()
    invariants, summary, state = _verify(rows, evidence)
    assert missing_invariants(invariants) == []
    assert {item.name for item in invariants} >= set(REQUIRED_INVARIANTS)
    assert all(item.status == "PASS" for item in invariants), as_dicts(invariants)
    assert _status(invariants, "workload_volume") == "PASS"
    assert summary["permanent_write_losses"] == 0
    assert summary["duplicate_records"] == 0
    assert summary["owner_restarts"] == 3
    assert state["writes"].keys() == {"w0-add-1", "w0-update-9", "w0-disposable-3", "w0-delete-4"}
    assert overall_status(invariants, duration_seconds=480.0, target_seconds=480)[0] == "PASS"


def test_a_missing_point_is_a_permanent_write_loss():
    """An acknowledged write that never reached Qdrant must fail the run."""
    rows, evidence = _healthy()
    evidence.points = [point for point in evidence.points if point["id"] != RECORD_ID]
    invariants, summary, _ = _verify(rows, evidence)
    assert _status(invariants, "write_durability") == "FAIL"
    assert summary["permanent_write_losses"] >= 1
    assert _status(invariants, "no_unexplained_loss") == "FAIL"


def test_a_stale_revision_is_not_accepted_as_durable():
    """The stored revision must equal the last acknowledged revision."""
    rows, evidence = _healthy()
    evidence.points = copy.deepcopy(evidence.points)
    evidence.points[0]["payload"]["version"] = "v1"
    invariants, _, _ = _verify(rows, evidence)
    assert _status(invariants, "write_durability") == "FAIL"


def test_a_duplicate_record_fails_idempotency():
    """Two points for one logical fact mean a replay created a second record."""
    rows, evidence = _healthy()
    evidence.points = copy.deepcopy(evidence.points)
    evidence.points.append(copy.deepcopy(evidence.points[0]))
    evidence.points[-1]["id"] = "55555555-5555-5555-8555-555555555555"
    invariants, summary, _ = _verify(rows, evidence)
    assert _status(invariants, "request_id_idempotency") == "FAIL"
    assert summary["duplicate_records"] >= 1


def test_a_deleted_record_that_reappears_fails_durability():
    """A resurrected delete is a correctness failure, not a metric."""
    rows, evidence = _healthy()
    evidence.points = copy.deepcopy(evidence.points)
    evidence.points.append(
        {
            "id": DELETED_ID,
            "payload": {"text": "Fact SOAK-w0-3-zz99", "version": "d1", "content_hash": "x"},
        }
    )
    invariants, summary, _ = _verify(rows, evidence)
    assert _status(invariants, "write_durability") == "FAIL"
    assert summary["permanent_write_losses"] >= 1


def test_a_stale_replay_does_not_overwrite_a_newer_acknowledged_revision():
    """The newest acknowledged sequence wins, not the last journal line."""
    rows, evidence = _healthy()
    rows = [
        *rows,
        {
            "kind": "op",
            "op": "update",
            "tool": "update",
            "identity": "hermes-b",
            "request_id": "w0-update-20",
            "record_id": RECORD_ID,
            "version": "v3",
            "result": "ok",
            "text": TEXT_V3,
            "seq": 20,
        },
        # A recovery drain replays an older identifier after that update.
        {
            "kind": "op",
            "op": "update",
            "tool": "update",
            "identity": "hermes-b",
            "request_id": "w0-update-9",
            "record_id": RECORD_ID,
            "version": "v2",
            "result": "ok",
            "text": TEXT_V2,
            "phase": "recovery",
            "seq": 9,
        },
    ]
    grants, _ = build_grants()
    evidence.ledger = _ledger(grants)
    evidence.ledger["requests"].append(
        {
            "request_key": request_receipt_key(
                next(grant for grant in grants if grant["principal"] == "hermes-b"), "w0-update-20"
            ),
            "done": 1,
            "plan": "null",
            "input_hash": "x",
            "principal": "hermes-b",
        }
    )
    evidence.points = copy.deepcopy(evidence.points)
    evidence.points[0]["payload"].update(
        text=TEXT_V3, version="v3", content_hash=content_hash(TEXT_V3)
    )
    invariants, _, _ = _verify(rows, evidence)
    assert _status(invariants, "write_durability") == "PASS"


def test_a_failed_read_is_an_availability_event_not_a_lost_write():
    """A throttled search is not a durability failure and is not recovered."""
    rows, evidence = _healthy()
    rows = rows + [
        {
            "kind": "op",
            "op": "search",
            "tool": "search",
            "identity": "codex",
            "request_id": "w0-search-9",
            "result": "error",
            "error": {
                "type": "RemoteStatusError",
                "reason": "temporarily_unavailable",
                "retryable": True,
            },
        }
    ]
    invariants, summary, state = _verify(rows, evidence)
    assert state["unresolved"] == []
    assert _status(invariants, "write_durability") == "PASS"
    assert summary["expected_transient_failures"] == 1


def test_a_failed_probe_fails_the_run():
    """An injected probe that fails is a finding, not a diagnostic footnote."""
    rows, evidence = _healthy()
    rows = rows + [{"kind": "probe", "probe": "lost_response", "status": "FAIL"}]
    invariants, _, _ = _verify(rows, evidence)
    assert _status(invariants, "probe_results") == "FAIL"
    rows, evidence = _healthy()
    rows = rows + [{"kind": "probe", "probe": "lost_response", "status": "INCOMPLETE"}]
    invariants, _, _ = _verify(rows, evidence)
    assert _status(invariants, "probe_results") == "INCOMPLETE"


def test_a_leaked_private_row_fails_isolation():
    """A private record visible to another credential must fail the run."""
    rows, _ = _healthy()
    repaired = copy.deepcopy(rows)
    for row in repaired:
        if row.get("probe") == "authorization":
            row["cross_scope_hidden"] = False
    _, evidence = _healthy()
    invariants, summary, _ = _verify(repaired, evidence)
    assert _status(invariants, "scope_isolation") == "FAIL"
    assert summary["cross_scope_leaks"] == 1


def test_an_unresolved_retryable_failure_is_not_excused():
    """A write that never received an acknowledgement is an unexplained loss."""
    rows, evidence = _healthy()
    rows = rows + [
        {
            "kind": "op",
            "op": "add",
            "tool": "add",
            "identity": "hermes-a",
            "request_id": "w0-add-77",
            "result": "error",
            "error": {"type": "RemoteStatusError", "reason": "timeout", "retryable": True},
            "text": "Fact SOAK-w0-77-aa11: the outbox path never confirmed this write.",
            "args": {"text": "Fact SOAK-w0-77-aa11"},
        }
    ]
    invariants, summary, _ = _verify(rows, evidence)
    assert _status(invariants, "write_durability") == "FAIL"
    assert summary["unexpected_failures"] == 0
    assert _status(invariants, "no_unexplained_loss") == "FAIL"


def test_a_replayed_event_that_later_commits_is_not_unresolved():
    """An event retried under the same event_id is resolved by its acknowledgement."""
    rows, evidence = _healthy()
    rows = rows + [
        {
            "kind": "op",
            "op": "event",
            "tool": "event",
            "identity": "hermes-a",
            "event_id": EVENT_ID,
            "result": "error",
            "error": {"type": "RemoteStatusError", "reason": "timeout", "retryable": True},
        }
    ]
    invariants, summary, state = _verify(rows, evidence)
    assert state["unresolved"] == []
    assert _status(invariants, "write_durability") == "PASS"
    assert summary["recovery_required"] == 0


def test_a_handshake_reset_inside_a_client_fault_window_is_an_expected_outage():
    """A reset before the request reaches the Owner is an outage, not a lost write."""
    rows, evidence = _healthy()
    rows = rows + [
        {
            "kind": "fault",
            "phase": "begin",
            "fault": "client_reset_peer",
            "ts": "2026-10-30T10:00:00.000Z",
        },
        {
            "kind": "op",
            "op": "add",
            "tool": "add",
            "identity": "hermes-a",
            "request_id": "w0-add-90",
            "result": "error",
            "ts": "2026-10-30T10:00:01.000Z",
            "error": {"type": "ConnectError", "reason": "tls_failed", "retryable": False},
        },
        {
            "kind": "fault",
            "phase": "end",
            "fault": "client_reset_peer",
            "ts": "2026-10-30T10:00:05.000Z",
        },
    ]
    invariants, summary, state = _verify(rows, evidence)
    assert state["unexpected"] == []
    assert len(state["path_outages"]) == 1
    assert summary["expected_path_outages"] == 1
    assert _status(invariants, "no_unexplained_loss") == "PASS"


def test_a_handshake_reset_outside_a_fault_window_stays_unexpected():
    """The same answer without a declared fault still fails the run."""
    rows, evidence = _healthy()
    rows = rows + [
        {
            "kind": "op",
            "op": "add",
            "tool": "add",
            "identity": "hermes-a",
            "request_id": "w0-add-91",
            "result": "error",
            "ts": "2026-10-30T10:10:00.000Z",
            "error": {"type": "ConnectError", "reason": "tls_failed", "retryable": False},
        }
    ]
    invariants, summary, _ = _verify(rows, evidence)
    assert summary["unexpected_failures"] == 1
    assert _status(invariants, "no_unexplained_loss") == "FAIL"


def test_a_non_retryable_product_failure_is_unexpected():
    """A business error during ordinary work is a product failure, not noise."""
    rows, evidence = _healthy()
    rows = rows + [
        {
            "kind": "op",
            "op": "add",
            "tool": "add",
            "identity": "hermes-a",
            "request_id": "w0-add-78",
            "result": "error",
            "error": {"type": "ValueError", "reason": "business_error", "retryable": False},
        }
    ]
    invariants, summary, _ = _verify(rows, evidence)
    assert summary["unexpected_failures"] == 1
    assert _status(invariants, "no_unexplained_loss") == "FAIL"


def test_a_missing_receipt_row_fails_idempotency():
    """Without the durable receipt the replay proof is incomplete."""
    rows, evidence = _healthy()
    evidence.ledger = copy.deepcopy(evidence.ledger)
    evidence.ledger["requests"] = evidence.ledger["requests"][1:]
    invariants, _, _ = _verify(rows, evidence)
    assert _status(invariants, "request_id_idempotency") == "FAIL"


def test_quarantined_events_and_pending_work_fail_the_ledger():
    """Retained or pending ledger work is never a passing window."""
    rows, evidence = _healthy()
    evidence.ledger = copy.deepcopy(evidence.ledger)
    evidence.ledger["events"].append({"key": "w0-event-9", "status": "QUARANTINED"})
    evidence.ledger["operations"].append({"key": "op2", "status": "PENDING"})
    invariants, summary, _ = _verify(rows, evidence)
    assert _status(invariants, "ledger_integrity") == "FAIL"
    assert _status(invariants, "no_unexplained_loss") == "FAIL"
    assert summary["ledger_status"]["quarantined"] == 1


def test_an_unavailable_ledger_is_incomplete_not_pass():
    """Absent evidence must never read as success."""
    rows, evidence = _healthy()
    evidence.ledger = {"available": False, "integrity": "missing"}
    evidence.integrity = "missing"
    invariants, _, _ = _verify(rows, evidence)
    assert _status(invariants, "ledger_integrity") == "INCOMPLETE"
    assert overall_status(invariants, duration_seconds=480.0, target_seconds=480)[0] != "PASS"


def test_a_short_window_and_a_harness_error_are_reported_as_such():
    """The measured window and the harness verdict are separate evidence."""
    rows, evidence = _healthy()
    invariants, _, _ = _verify(rows, evidence, duration_seconds=120.0)
    assert _status(invariants, "window_complete") == "INCOMPLETE"
    assert overall_status(invariants, duration_seconds=120.0, target_seconds=480)[0] == "INCOMPLETE"
    rows = rows + [{"kind": "harness", "event": "harness_error", "error": {"type": "RuntimeError"}}]
    invariants, _, _ = _verify(rows, evidence)
    assert _status(invariants, "harness_clean") == "FAIL"
    assert (
        overall_status(invariants, duration_seconds=480.0, target_seconds=480, harness_error="x")[0]
        == "HARNESS_ERROR"
    )


def test_a_restart_that_never_became_ready_fails_the_lease():
    """One Owner at a time is a precondition for the whole window."""
    rows, evidence = _healthy()
    invariants, _, _ = _verify(rows, evidence, owner_starts=3, owner_restarts=3)
    assert _status(invariants, "writer_lease") == "FAIL"
    invariants, _, _ = _verify(rows, evidence, start_failures=1)
    assert _status(invariants, "writer_lease") == "FAIL"


def test_retrieval_quality_is_measured_separately_from_loss():
    """A retrieval miss is reported, never converted into a lost write."""
    rows, evidence = _healthy()
    rows = (
        rows
        + [
            {
                "kind": "retrieval",
                "identity": "codex",
                "marker": "m",
                "record_id": RECORD_ID,
                "hit": False,
            }
        ]
        * 4
    )
    invariants, summary, _ = _verify(rows, evidence)
    assert _status(invariants, "write_durability") == "PASS"
    assert summary["retrieval"]["queries"] == 5
    assert summary["retrieval"]["hits"] == 1
    assert _status(invariants, "retrieval_quality") == "FAIL"


def test_required_invariant_names_are_unique_and_complete():
    """A duplicated or missing invariant name would mask a gap."""
    invariants = [Invariant(name=name, status="PASS") for name in REQUIRED_INVARIANTS]
    assert len(set(REQUIRED_INVARIANTS)) == len(REQUIRED_INVARIANTS) >= 10
    assert missing_invariants(invariants) == []
    assert missing_invariants(invariants[:-1]) == [REQUIRED_INVARIANTS[-1]]
