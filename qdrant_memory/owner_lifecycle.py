"""Trusted lifecycle admission into the existing Owner Runtime and Ledger.

Host adapters supply permitted turn content and an immutable event ID only.
Identity, space, capture policy, extraction and persistence remain Owner-side.
"""

import json
import math

from .event_quarantine import EventQuarantined
from .models import digest, now

_TURN_FIELDS = {"event_id", "session_id", "kind", "user", "assistant"}
_CANDIDATE_FIELDS = {"event_id", "session_id", "kind", "candidate_schema", "candidates"}
_CANDIDATE_CONTEXT_FIELDS = _CANDIDATE_FIELDS | {"replacement_context"}
_CANDIDATE_SCHEMA = "qdrant-memory/extraction-v1"
_MAX_CANDIDATES = 32
_MAX_CANDIDATE_TEXT = 4096


def _check_candidate(candidate):
    """Accept only one bounded record in the Runtime extraction schema."""
    if not isinstance(candidate, dict) or set(candidate) != {
        "text",
        "category",
        "importance",
        "replacement_intent",
    }:
        return False
    text = candidate["text"]
    category = candidate["category"]
    importance = candidate["importance"]
    try:
        valid_importance = (
            isinstance(importance, (int, float))
            and not isinstance(importance, bool)
            and math.isfinite(importance)
            and 0 <= importance <= 1
        )
    except OverflowError:
        valid_importance = False
    return (
        isinstance(text, str)
        and 0 < len(text.strip()) <= _MAX_CANDIDATE_TEXT
        and isinstance(category, list)
        and len(category) <= 8
        and all(isinstance(item, str) and 0 < len(item) <= 64 for item in category)
        and valid_importance
        and isinstance(candidate["replacement_intent"], bool)
    )


def _check_turn(event):
    """Reject anything except a bounded raw turn or versioned candidate event."""
    if not isinstance(event, dict):
        raise ValueError("Invalid lifecycle event")
    raw_turn = set(event) == _TURN_FIELDS
    candidate_fields = set(event)
    candidates = candidate_fields in (_CANDIDATE_FIELDS, _CANDIDATE_CONTEXT_FIELDS)
    valid = (
        (raw_turn or candidates)
        and event.get("kind") == "turn_completed"
        and isinstance(event.get("event_id"), str)
        and 0 < len(event["event_id"]) <= 128
        and isinstance(event.get("session_id"), str)
        and 0 < len(event["session_id"]) <= 256
    )
    if valid and raw_turn:
        valid = all(isinstance(event[key], str) for key in ("user", "assistant"))
    elif valid and candidates:
        candidate_list = event.get("candidates")
        valid = (
            event.get("candidate_schema") == _CANDIDATE_SCHEMA
            and isinstance(candidate_list, list)
            and len(candidate_list) <= _MAX_CANDIDATES
            and all(_check_candidate(item) for item in candidate_list)
        )
        has_replacement = valid and any(item["replacement_intent"] for item in candidate_list)
        if has_replacement:
            context = event.get("replacement_context")
            valid = (
                candidate_fields == _CANDIDATE_CONTEXT_FIELDS
                and isinstance(context, str)
                and 0 < len(context.strip()) <= 2048
            )
        else:
            valid = valid and candidate_fields == _CANDIDATE_FIELDS
    if valid:
        try:
            valid = len(json.dumps(event, ensure_ascii=False).encode("utf-8")) <= 131072
        except UnicodeEncodeError:
            valid = False
    if not valid:
        raise ValueError("Invalid lifecycle event")


class OwnerLifecycle:
    """Coordinate durable events without implementing another extraction runtime."""

    def __init__(self, owner):
        """Bind one existing Owner; lifecycle is an explicit opt-in extension."""
        self.owner = owner
        ledger = owner.runtime.ledger
        with ledger.lock, ledger.db:
            ledger.db.execute("""CREATE TABLE IF NOT EXISTS owner_events_v1 (
                collection TEXT NOT NULL, request_key TEXT NOT NULL,
                input_hash TEXT NOT NULL, event_key TEXT NOT NULL, event_json TEXT NOT NULL,
                PRIMARY KEY(collection, request_key))""")

    def _authority(self, token):
        """Resolve current write/capture authority for submission and event controls."""
        self.owner.runtime.check_active()
        grant = self.owner.authenticate(token)
        if (
            grant["permission"] != "write"
            or "logical_agent_id" not in grant
            or (grant["visibility"] != "private" and not grant.get("capture_rule_id"))
        ):
            raise PermissionError("Automatic capture requires explicit authorized policy")
        if self.owner.admission_check is not None:
            self.owner.admission_check()
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
        return authority

    def _receipt(self, token, event_id):
        """Look up only this device/capture rule's receipt; never return its content."""
        if not isinstance(event_id, str) or not 0 < len(event_id) <= 128:
            raise ValueError("Invalid lifecycle event ID")
        authority = self._authority(token)
        ledger = self.owner.runtime.ledger
        receipt = ledger.db.execute(
            "SELECT * FROM owner_events_v1 WHERE collection=? AND request_key=?",
            (ledger.collection, digest([authority, event_id])),
        ).fetchone()
        row = ledger.row("events", receipt["event_key"]) if receipt is not None else None
        return receipt, row

    def inspect(self, token, event_id):
        """Inspect one's own durable event status without exposing memory or errors."""
        with self.owner.runtime.lock:
            receipt, row = self._receipt(token, event_id)
            status = "NOT_FOUND"
            if receipt is not None:
                status = row["status"] if row is not None else "ACCEPTED"
            return {"event_id": event_id, "status": status}

    def retry(self, token, event_id):
        """Re-enqueue this device's durable immutable plan without returning its content."""
        rt = self.owner.runtime
        ledger = rt.ledger
        with rt.lock:
            receipt, _ = self._receipt(token, event_id)
            if receipt is None:
                return {"event_id": event_id, "status": "NOT_FOUND"}
            value, event_key, status = self._replayed_plan(ledger, receipt, receipt["input_hash"])
            if status is not None:
                return {"event_id": event_id, "status": status}
            if not isinstance(value, dict) or value.get("host_event_id") != event_id:
                raise ValueError("Lifecycle event plan is unavailable")
            status = self._run_plan(rt, ledger, receipt["request_key"], value, event_key)
            return {"event_id": event_id, "status": status}

    def cancel(self, token, event_id):
        """Cancel only raw input; any prepared/uncertain write must still reconcile."""
        rt = self.owner.runtime
        with rt.lock:
            receipt, row = self._receipt(token, event_id)
            if receipt is None:
                raise KeyError("Lifecycle event was not found")
            if row is None:
                raise ValueError("Accepted event requires recovery before cancellation")
            if row["status"] == "CANCELLED":
                return {"event_id": event_id, "status": "CANCELLED"}
            value = json.loads(row["payload_json"])
            if row["status"] not in {"PENDING", "FAILED", "QUARANTINED"} or any(
                key in value for key in ("candidates", "operation_keys")
            ):
                raise ValueError("Event is prepared or committed; recover writes instead")
            with rt.ledger.lock, rt.ledger.db:
                updated = rt.ledger.db.execute(
                    "UPDATE events SET status='CANCELLED',payload_json='{}',last_error=NULL,committed_at=? "
                    "WHERE collection=? AND event_id=? AND status=? AND payload_json=?",
                    (
                        now(),
                        rt.ledger.collection,
                        receipt["event_key"],
                        row["status"],
                        row["payload_json"],
                    ),
                ).rowcount
                if updated != 1:
                    raise ValueError("Event is prepared or committed; recover writes instead")
                rt.ledger.db.execute(
                    "UPDATE owner_events_v1 SET event_json='{}' WHERE collection=? AND request_key=?",
                    (rt.ledger.collection, receipt["request_key"]),
                )
            return {"event_id": event_id, "status": "CANCELLED"}

    def submit(self, token, event):
        """Authenticate and durably accept one turn before extraction or database I/O."""
        _check_turn(event)
        rt = self.owner.runtime
        ledger = rt.ledger
        with rt.lock:
            authority = self._authority(token)
            if self.owner.blocked:
                from .shared_state import SharedRecoveryRequiredError

                raise SharedRecoveryRequiredError()
            if rt.extractor is None:
                raise RuntimeError("Owner relation-review provider must be explicitly configured")
            request_key = digest([authority, event["event_id"]])
            receipt = ledger.db.execute(
                "SELECT * FROM owner_events_v1 WHERE collection=? AND request_key=?",
                (ledger.collection, request_key),
            ).fetchone()
            if receipt is not None:
                value, event_key, status = self._replayed_plan(ledger, receipt, digest(event))
                if status is not None:
                    return {"event_id": event["event_id"], "status": status}
            else:
                value, event_key = self._new_plan(
                    ledger, request_key, digest(event), authority, event
                )
            status = self._run_plan(rt, ledger, request_key, value, event_key)
            return {"event_id": event["event_id"], "status": status}

    def _replayed_plan(self, ledger, receipt, input_hash):
        """Reuse a stored immutable plan, refusing a reused host event ID."""
        if receipt["input_hash"] != input_hash:
            raise ValueError("Lifecycle event ID was already used")
        value = json.loads(receipt["event_json"])
        event_key = receipt["event_key"]
        existing = ledger.row("events", event_key)
        if existing is not None and existing["status"] in {"COMMITTED", "CANCELLED"}:
            return value, event_key, existing["status"]
        return value, event_key, None

    def _new_plan(self, ledger, request_key, input_hash, authority, event):
        """Refuse a full backlog, then durably receipt the plan a new turn runs."""
        queued = sum(
            ledger.db.execute(
                "SELECT COUNT(*) FROM events WHERE collection=? AND status IN ('PENDING','FAILED','QUARANTINED')",
                (ledger.collection,),
            ).fetchone()
        )
        if queued >= 128:
            raise RuntimeError("Owner lifecycle backlog is full")
        value = {
            "kind": "turn",
            "source": "owner_lifecycle",
            "scope": self.owner.scope(authority).as_dict(),
            "session_id": digest([authority, event["session_id"]]),
            "host_event_id": event["event_id"],
            "owner_authority": authority,
        }
        if event.get("candidate_schema") == _CANDIDATE_SCHEMA:
            value.update(
                candidate_schema=event["candidate_schema"],
                candidates=event["candidates"],
            )
            if "replacement_context" in event:
                value["user"] = event["replacement_context"]
        else:
            value.update(user=event["user"], assistant=event["assistant"])
        event_key = digest([ledger.collection, value])
        with ledger.lock, ledger.db:
            ledger.db.execute(
                "INSERT INTO owner_events_v1 VALUES(?,?,?,?,?)",
                (ledger.collection, request_key, input_hash, event_key, json.dumps(value)),
            )
        return value, event_key

    def _run_plan(self, rt, ledger, request_key, value, event_key):
        """Enqueue one accepted plan and return its settled event status."""
        # The receipt's immutable event plan closes the crash-before-enqueue gap.
        ledger.enqueue_event(value)
        try:
            rt.process_event(event_key)
        except EventQuarantined:
            # A retained event holds no plan, so unrelated writes continue.
            return ledger.row("events", event_key)["status"]
        except Exception:
            self.owner.blocked = True
            raise
        row = ledger.row("events", event_key)
        if row["status"] == "COMMITTED":
            with ledger.lock, ledger.db:
                ledger.db.execute(
                    "UPDATE owner_events_v1 SET event_json='{}' WHERE collection=? AND request_key=?",
                    (ledger.collection, request_key),
                )
        return row["status"]

    def recover(self):
        """Re-enqueue accepted immutable plans after an Owner crash; use Runtime recovery."""
        rt = self.owner.runtime
        with rt.lock:
            for row in rt.ledger.db.execute(
                "SELECT * FROM owner_events_v1 WHERE collection=? AND event_json!='{}'",
                (rt.ledger.collection,),
            ).fetchall():
                rt.ledger.enqueue_event(json.loads(row["event_json"]))
            failures = rt.recover()
            if failures:
                self.owner.blocked = True
            with rt.ledger.lock, rt.ledger.db:
                rt.ledger.db.execute(
                    "UPDATE owner_events_v1 SET event_json='{}' WHERE collection=? AND event_key IN "
                    "(SELECT event_id FROM events WHERE collection=? AND status='COMMITTED')",
                    (rt.ledger.collection, rt.ledger.collection),
                )
            return failures


def record_filters(event):
    """Restrict lifecycle dedupe to records admitted under the same authority."""
    from .shared_protocol import MARKER

    authority = event["owner_authority"]
    filters = {
        "shared_protocol": MARKER,
        "space_id": authority["space_id"],
        "visibility": authority["visibility"],
    }
    if authority["visibility"] == "private":
        filters["logical_agent_id"] = authority["logical_agent_id"]
    return filters


def record_fields(event, old, identifier):
    """Attach only authority captured by the Owner, never extractor-produced metadata."""
    authority = event.get("owner_authority")
    if event.get("source") != "owner_lifecycle" or not isinstance(authority, dict):
        return {}
    from .shared_protocol import MARKER

    previous = old.payload if old else {}
    if old and any(previous.get(key) != value for key, value in record_filters(event).items()):
        raise PermissionError("Lifecycle target is outside authorized records")
    return {
        "shared_protocol": MARKER,
        "space_id": authority["space_id"],
        "visibility": authority["visibility"],
        "logical_agent_id": authority["logical_agent_id"],
        "created_by": previous.get("created_by", authority["principal"]),
        "updated_by": authority["principal"],
        "created_by_device": previous.get("created_by_device", authority["device_id"]),
        "updated_by_device": authority["device_id"],
        "version": digest([event["event_id"], identifier]),
        "capture_rule_id": authority["capture_rule_id"],
    }
