"""Verify a soak window against hard invariants and real storage state.

The verifier never treats "the process exited" or "the search returned a hit" as
proof. It compares the acknowledged journal with the ledger rows and with the
exact Qdrant points, so a lost write, a duplicate row or a cross-scope leak
fails the run.
"""

import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from qdrant_memory.models import content_hash, digest

from .common import REQUIRED_INVARIANTS, Invariant, percentile, read_jsonl, trend

RETRIEVAL_FLOOR = 0.5
PATH_FAULT_REASONS = frozenset({"tls_failed", "unreachable"})


@dataclass
class Evidence:
    """Everything the verifier reads, independent of how it was collected."""

    points: list = field(default_factory=list)
    ledger: dict = field(default_factory=dict)
    integrity: str = "unknown"
    collected: bool = True
    error: str = ""


class QdrantEvidence:
    """Read the run collection through the TLS listener with the admin key."""

    def __init__(self, endpoint, api_key, collection, ca_file):
        """Bind the admin endpoint and the exact collection."""
        self.endpoint = endpoint
        self.api_key = api_key
        self.collection = collection
        self.ca_file = ca_file

    def read(self):
        """Return every point payload in the collection."""
        import os

        from qdrant_client import QdrantClient

        previous = os.environ.get("SSL_CERT_FILE")
        if self.ca_file is not None:
            os.environ["SSL_CERT_FILE"] = str(self.ca_file)
        try:
            client = QdrantClient(url=self.endpoint, api_key=self.api_key, timeout=30)
            try:
                points, offset = [], None
                while True:
                    rows, offset = client.scroll(
                        self.collection,
                        limit=256,
                        offset=offset,
                        with_payload=True,
                        with_vectors=False,
                    )
                    points.extend(
                        {"id": str(row.id), "payload": dict(row.payload or {})} for row in rows
                    )
                    if offset is None:
                        return points
            finally:
                client.close()
        finally:
            if self.ca_file is None:
                pass
            elif previous is None:
                os.environ.pop("SSL_CERT_FILE", None)
            else:
                os.environ["SSL_CERT_FILE"] = previous


def copy_ledger(home, target):
    """Copy the ledger and its sidecars so evidence survives a later cleanup."""
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    source = Path(home) / "qdrant-memory" / "state.db"
    copied = []
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(source) + suffix)
        if path.exists():
            shutil.copy2(path, target / path.name)
            copied.append(path.name)
    return {"source": str(source), "copied": copied, "directory": str(target)}


def read_ledger(directory, collection):
    """Read the copied ledger read-only and return the rows that matter."""
    path = Path(directory) / "state.db"
    if not path.exists():
        return {"available": False, "integrity": "missing"}
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        result = {"available": True, "integrity": integrity, "collection": collection}
        for table, key in (
            ("operations", "operations"),
            ("events", "events"),
            ("shared_requests_v1", "requests"),
            ("owner_events_v1", "owner_events"),
        ):
            if table not in tables:
                result[key] = []
                continue
            if table == "shared_requests_v1":
                rows = connection.execute(
                    "SELECT request_key, input_hash, done, principal, plan FROM shared_requests_v1 "
                    "WHERE collection=?",
                    (collection,),
                ).fetchall()
            elif table == "owner_events_v1":
                rows = connection.execute(
                    "SELECT request_key, event_key, event_json FROM owner_events_v1 WHERE collection=?",
                    (collection,),
                ).fetchall()
            elif table == "operations":
                rows = connection.execute(
                    "SELECT idempotency_key AS key, status, attempts, last_error FROM operations "
                    "WHERE collection=?",
                    (collection,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT event_id AS key, status, attempts, last_error FROM events WHERE collection=?",
                    (collection,),
                ).fetchall()
            result[key] = [dict(row) for row in rows]
        return result
    finally:
        connection.close()


def event_receipt_key(grant, event_id):
    """Recompute the Owner's lifecycle receipt key for one event."""
    authority = {
        key: grant[key]
        for key in (
            "principal",
            "owner_id",
            "space_id",
            "visibility",
            "logical_agent_id",
            "device_id",
        )
    }
    authority["capture_rule_id"] = grant.get("capture_rule_id", "private-default")
    return digest([authority, event_id])


def request_receipt_key(grant, request_id):
    """Recompute the Owner's tool receipt key for one request."""
    identity = [grant["owner_id"], grant["space_id"], grant["principal"]]
    if "logical_agent_id" in grant:
        identity.extend([grant["logical_agent_id"], grant["device_id"]])
    return digest([*identity, request_id])


def fault_windows(rows):
    """Return ``(start, end, name)`` for every closed fault window in the journal."""
    open_windows, windows = {}, []
    for row in rows:
        if row.get("kind") != "fault":
            continue
        name = str(row.get("fault") or "")
        if row.get("phase") == "begin":
            open_windows.setdefault(name, []).append(row.get("ts"))
        elif row.get("phase") == "end" and open_windows.get(name):
            windows.append((open_windows[name].pop(), row.get("ts"), name))
    return windows


PATH_FAULTS = ("client_disconnect", "client_reset_peer")


def inside_client_fault(moment, windows):
    """Return whether one failure happened while the client path was cut."""
    for start, end, name in windows:
        if name in PATH_FAULTS and start and end and start <= moment <= end:
            return True
    return False


def acknowledged_state(rows):
    """Rebuild the caller-visible state and the failure classification.

    A connection reset during the TLS handshake never reaches the Owner, so a
    terminal ``tls_failed`` answer inside a declared client-path fault window is
    an expected path outage rather than an unexplained product failure. The same
    answer outside a fault window stays a failure.
    """
    records, writes, events, probes = {}, {}, {}, []
    attempts = successes = 0
    failures = []
    for row in rows:
        kind = row.get("kind")
        if kind == "attempt":
            attempts += 1
            continue
        if kind == "probe":
            probes.append(row)
            continue
        if kind != "op":
            continue
        operation = row.get("op")
        if row.get("result") != "ok":
            if operation != "probe":
                failures.append(row)
            continue
        successes += 1
        request_id = row.get("request_id")
        if operation in {"add", "update", "delete"}:
            writes[request_id] = row
            record_id = row.get("record_id")
            if not record_id:
                continue
            # A replayed receipt carries an older revision than a later accepted
            # update, so the newest acknowledged sequence wins, not the last line.
            seen = records.get(record_id)
            if seen is not None and (seen.get("seq") or 0) > (row.get("seq") or 0):
                continue
            state = records.setdefault(
                record_id,
                {"text": "", "version": "", "action": operation.upper(), "deleted": False},
            )
            state["action"] = operation.upper()
            state["seq"] = row.get("seq") or 0
            if operation == "delete":
                state["deleted"] = True
            else:
                state.update(
                    text=row.get("text", ""), version=row.get("version", ""), deleted=False
                )
        elif operation == "event":
            events[row.get("event_id")] = row
    unresolved = []
    for row in failures:
        if row.get("error", {}).get("retryable") is not True:
            continue
        if row.get("op") == "event":
            if row.get("event_id") not in events:
                unresolved.append(row)
        elif row.get("tool") in {"add", "update", "delete"}:
            # A failed read is an availability event; it cannot lose a write.
            if row.get("request_id") not in writes:
                unresolved.append(row)
    windows = fault_windows(rows)
    unexpected, path_outages = [], []
    for row in failures:
        if (row.get("error") or {}).get("retryable") is True:
            continue
        reason = (row.get("error") or {}).get("reason")
        # Only a failure that cannot have reached the Owner is excused here.
        # ``outcome_unknown`` may have committed, so it needs a proven replay.
        if reason in PATH_FAULT_REASONS and inside_client_fault(row.get("ts"), windows):
            path_outages.append(row)
        else:
            unexpected.append(row)
    expected_hashes = {}
    for record_id, record in records.items():
        if not record.get("deleted") and record.get("text"):
            expected_hashes[record_id] = content_hash(record["text"])
    for event_id, row in events.items():
        if row.get("event_text"):
            expected_hashes["event:" + str(event_id)] = content_hash(row["event_text"])
    for probe in probes:
        if isinstance(probe.get("text"), str) and probe["text"]:
            expected_hashes["probe:" + str(probe.get("probe"))] = content_hash(probe["text"])
    return {
        "records": records,
        "writes": writes,
        "events": events,
        "probes": probes,
        "attempts": attempts,
        "successes": successes,
        "failures": failures,
        "unresolved": unresolved,
        "unexpected": unexpected,
        "path_outages": path_outages,
        "fault_windows": windows,
        "expected_hashes": expected_hashes,
    }


def verify(
    rows,
    evidence,
    *,
    grants,
    identity,
    duration_seconds,
    target_seconds,
    owner_restarts,
    owner_starts,
    start_failures,
):
    """Return the invariant set, the summary counts and the rebuilt state."""
    state = acknowledged_state(rows)
    invariants = []

    def add(name, status, detail="", **extra):
        """Append one invariant with its measured evidence."""
        invariants.append(Invariant(name=name, status=status, detail=detail, evidence=extra))

    payloads = {point["id"]: point["payload"] for point in evidence.points}
    present = {key: value for key, value in state["records"].items() if not value.get("deleted")}
    missing, mismatched = [], []
    for record_id, record in present.items():
        payload = payloads.get(record_id)
        if payload is None:
            missing.append(record_id)
            continue
        if payload.get("text") != record["text"] or payload.get("version") != record["version"]:
            mismatched.append(record_id)
        elif (payload.get("content_hash") or "") != state["expected_hashes"].get(record_id, ""):
            mismatched.append(record_id)
    resurrected = [
        record_id
        for record_id, record in state["records"].items()
        if record.get("deleted") and record_id in payloads
    ]
    add(
        "write_durability",
        "PASS" if not (missing or mismatched or resurrected or state["unresolved"]) else "FAIL",
        "Every acknowledged write is present with its acknowledged revision.",
        missing=len(missing),
        mismatched=len(mismatched),
        resurrected=len(resurrected),
        unresolved=len(state["unresolved"]),
        acknowledged=len(present),
    )

    grants_by_principal = {grant["principal"]: grant for grant in grants}
    rows_by_key = {row["request_key"]: row for row in evidence.ledger.get("requests", [])}
    missing_rows = []
    for request_id, row in state["writes"].items():
        grant = grants_by_principal.get(row.get("identity", ""))
        key = request_receipt_key(grant, request_id) if grant else ""
        receipt = rows_by_key.get(key)
        if receipt is None or not receipt.get("done") or (receipt.get("plan") or "null") != "null":
            missing_rows.append(request_id)
    hash_counts = {}
    for payload in payloads.values():
        value = payload.get("content_hash")
        if value:
            hash_counts[value] = hash_counts.get(value, 0) + 1
    duplicates = [
        key for key, value in state["expected_hashes"].items() if hash_counts.get(value, 0) > 1
    ]
    add(
        "request_id_idempotency",
        "PASS" if not (missing_rows or duplicates) else "FAIL",
        "Each request identifier produced exactly one receipt and one record.",
        receipts=len(rows_by_key),
        writes=len(state["writes"]),
        missing_receipts=len(missing_rows),
        duplicate_records=len(duplicates),
    )

    event_rows = {row["request_key"]: row for row in evidence.ledger.get("owner_events", [])}
    event_missing = []
    for event_id, row in state["events"].items():
        grant = grants_by_principal.get(row.get("identity", ""))
        key = event_receipt_key(grant, event_id) if grant else ""
        receipt = event_rows.get(key)
        text = row.get("event_text") or ""
        if receipt is None or receipt.get("event_json") != "{}":
            event_missing.append(event_id)
        elif text and hash_counts.get(content_hash(text), 0) != 1:
            event_missing.append(event_id)
    add(
        "event_id_idempotency",
        "PASS" if not event_missing else "FAIL",
        "Each lifecycle event produced one receipt and one memory.",
        events=len(state["events"]),
        missing=len(event_missing),
    )

    authorization = [probe for probe in state["probes"] if probe.get("probe") == "authorization"]
    add(
        "authorization",
        "PASS"
        if authorization and all(item.get("status") == "PASS" for item in authorization)
        else "INCOMPLETE",
        "Read-only writes are refused and private rows stay in their partition.",
        probes=len(authorization),
    )

    statuses = {str(probe.get("status")) for probe in state["probes"]}
    if not statuses:
        probe_status = "INCOMPLETE"
    elif "FAIL" in statuses:
        probe_status = "FAIL"
    elif statuses - {"PASS"}:
        probe_status = "INCOMPLETE"
    else:
        probe_status = "PASS"
    add(
        "probe_results",
        probe_status,
        "Every injected probe reported a verified result.",
        probes=len(state["probes"]),
        statuses=sorted(statuses),
    )

    leaks = sum(1 for probe in authorization if not probe.get("cross_scope_hidden", False))
    add(
        "scope_isolation",
        "PASS" if authorization and leaks == 0 else ("FAIL" if leaks else "INCOMPLETE"),
        "No private record was visible to another credential.",
        leaks=leaks,
    )

    ledger = evidence.ledger
    pending = [
        row
        for row in ledger.get("operations", []) + ledger.get("events", [])
        if row.get("status") in {"PENDING", "FAILED"}
    ]
    quarantined = [row for row in ledger.get("events", []) if row.get("status") == "QUARANTINED"]
    open_receipts = [row for row in ledger.get("requests", []) if not row.get("done")]
    if not ledger.get("available"):
        ledger_status = "INCOMPLETE"
    elif evidence.integrity != "ok" or pending or open_receipts:
        ledger_status = "FAIL"
    else:
        ledger_status = "PASS"
    add(
        "ledger_integrity",
        ledger_status,
        "The ledger passes its own integrity check with no pending work.",
        integrity=evidence.integrity,
        pending=len(pending),
        quarantined=len(quarantined),
        open_receipts=len(open_receipts),
    )

    add(
        "no_unexplained_loss",
        "PASS"
        if not (state["unresolved"] or quarantined or state["unexpected"] or missing or resurrected)
        else "FAIL",
        "No acknowledged write is missing and no failure is unexplained.",
        unexpected_failures=len(state["unexpected"]),
        quarantined=len(quarantined),
        unresolved=len(state["unresolved"]),
    )

    lease_ok = start_failures == 0 and owner_starts >= 1 and owner_starts == owner_restarts + 1
    add(
        "writer_lease",
        "PASS" if lease_ok else "FAIL",
        "One Owner process held the writer lease for the whole window.",
        restarts=owner_restarts,
        starts=owner_starts,
        start_failures=start_failures,
    )

    harness_failures = {
        "harness_error",
        "fault_error",
        "workload_step_error",
        "probe_error",
        "background_error",
        "run_aborted",
    }
    harness_errors = [
        row for row in rows if row.get("kind") == "harness" and row.get("event") in harness_failures
    ]
    add(
        "harness_clean",
        "PASS" if not harness_errors else "FAIL",
        "The harness itself reported no unhandled exception.",
        harness_errors=len(harness_errors),
    )

    scenario = getattr(identity, "scenario", "") if identity is not None else ""
    closed_faults = len(fault_windows(rows))
    required_faults = {
        "restart": max(1, int(target_seconds // 300)),
        "network": max(2, int(target_seconds // 300)),
    }.get(scenario, 0)
    if scenario == "restart":
        covered = owner_restarts >= required_faults
        detail = f"{owner_restarts} restart(s), {required_faults} required"
    elif scenario == "network":
        covered = closed_faults >= required_faults
        detail = f"{closed_faults} fault window(s), {required_faults} required"
    else:
        covered = True
        detail = "the steady scenario injects no faults"
    add(
        "scenario_coverage",
        "PASS" if covered else "FAIL",
        "The scenario actually injected what it claims to test.",
        scenario=scenario,
        restarts=owner_restarts,
        fault_windows=closed_faults,
        required=required_faults,
        detail_text=detail,
    )

    add(
        "window_complete",
        "PASS" if target_seconds and duration_seconds >= target_seconds * 0.95 else "INCOMPLETE",
        "The measured window covered the required workload duration.",
        duration_seconds=round(duration_seconds, 3),
        target_seconds=target_seconds,
    )

    acknowledged = len(state["writes"]) + len(state["events"])
    # One acknowledged write per 100 seconds, and never fewer than three.
    floor = max(3, int(target_seconds / 100))
    add(
        "workload_volume",
        "PASS" if acknowledged >= floor else "FAIL",
        "The window actually exercised write and event paths.",
        acknowledged=acknowledged,
        floor=floor,
    )

    operations = [row for row in rows if row.get("kind") == "op"]
    latencies = [row.get("duration_ms") for row in operations]
    # A replay may legitimately wait for Retry-After; report the clean path
    # separately so a throttle stall cannot hide the ordinary latency.
    clean = [row.get("duration_ms") for row in operations if not row.get("replays")]
    retrievals = [row for row in rows if row.get("kind") == "retrieval"]
    hits = sum(1 for row in retrievals if row.get("hit"))
    rate = (hits / len(retrievals)) if retrievals else None
    add(
        "retrieval_quality",
        "PASS" if (not retrievals or (rate is not None and rate >= RETRIEVAL_FLOOR)) else "FAIL",
        "Exact-content checks decide loss; retrieval quality is measured separately.",
        queries=len(retrievals),
        hits=hits,
        rate=round(rate, 4) if rate is not None else None,
    )

    metrics = [row for row in rows if row.get("kind") == "metric"]
    summary = {
        "total_requests": state["attempts"],
        "successful_requests": state["successes"],
        "expected_transient_failures": len(state["failures"])
        - len(state["unexpected"])
        - len(state["unresolved"]),
        "transient_attempts": sum(
            1
            for row in rows
            if row.get("kind") == "attempt" and (row.get("error") or {}).get("retryable") is True
        ),
        "retried_operations": sum(1 for row in operations if (row.get("replays") or 0) > 0),
        "unexpected_failures": len(state["unexpected"]),
        "expected_path_outages": len(state["path_outages"]),
        "permanent_write_losses": len(missing) + len(resurrected) + len(mismatched),
        "duplicate_records": len(duplicates),
        "cross_scope_leaks": leaks,
        "owner_restarts": owner_restarts,
        "recovery_required": sum(
            1
            for row in state["failures"]
            if (row.get("error") or {}).get("reason") == "recovery_required"
            or (row.get("error") or {}).get("type") == "SharedRecoveryRequiredError"
        ),
        "acknowledged_writes": len(state["writes"]),
        "acknowledged_events": len(state["events"]),
        "ledger_status": {
            "integrity": evidence.integrity,
            "operations": len(ledger.get("operations", [])),
            "events": len(ledger.get("events", [])),
            "requests": len(ledger.get("requests", [])),
            "pending": len(pending),
            "quarantined": len(quarantined),
        },
        "latency_p50_p95_p99": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
        },
        "latency_clean_p50_p95_p99": {
            "p50": percentile(clean, 0.50),
            "p95": percentile(clean, 0.95),
            "p99": percentile(clean, 0.99),
        },
        "retrieval": {"queries": len(retrievals), "hits": hits, "rate": rate},
        "rss_trend": trend([row.get("rss_bytes") for row in metrics if row.get("rss_bytes")]),
        "fd_trend": trend(
            [
                row.get("file_descriptors")
                for row in metrics
                if row.get("file_descriptors") is not None
            ]
        ),
        "points": len(evidence.points),
        "samples": len(metrics),
    }
    return invariants, summary, state


def load_journal(path):
    """Read the evidence stream used to rebuild the caller-visible state."""
    return read_jsonl(path)


def missing_invariants(invariants):
    """Return the required invariant names the run never produced."""
    names = {item.name for item in invariants}
    return [name for name in REQUIRED_INVARIANTS if name not in names]


def as_dicts(invariants):
    """Return the invariants as plain mappings for a report."""
    return [
        {
            "name": item.name,
            "status": item.status,
            "detail": item.detail,
            "evidence": json.loads(json.dumps(item.evidence)),
        }
        for item in invariants
    ]
