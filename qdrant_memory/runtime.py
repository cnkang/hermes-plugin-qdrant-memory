"""Serialized durable mutation engine, shared by provider and migration CLI."""

import json
import time
from threading import RLock

from .dedupe import decide
from .models import Scope, content_hash, enforce_limits, now, payload, point_id
from .progress import report
from .retry import run_with_retry


class WorkDeferred(RuntimeError):
    """Signal that durable pending work must be replayed by the next owner."""


class Runtime:
    """Serialize durable mutations shared by lifecycle hooks, tools and migration."""

    def __init__(self, cfg, store, ledger, extractor=None, stop_requested=None):
        """Bind dependencies and one reentrant mutation lock."""
        self.cfg, self.store, self.ledger, self.extractor = cfg, store, ledger, extractor
        self.lock = RLock()
        self.decisions = {"ADD": 0, "UPDATE": 0, "SKIP": 0}
        self.store.metrics = ledger
        self.stop_requested = stop_requested or (lambda: False)

    def check_active(self):
        """Defer another service phase after the owner's drain deadline."""
        if self.stop_requested():
            raise WorkDeferred("Durable work deferred for restart recovery")

    def operation(self, identifier, action, value, source_id="", source_version="", generation=""):
        """Prepare a size-checked mutation before any service write."""
        if action == "DELETE" and not source_version:
            source_version = now()
        if action != "DELETE":
            value = enforce_limits(value, self.cfg["limits"])
        return self.ledger.enqueue_operation(
            identifier, action, value, source_id, source_version, generation
        )

    def commit(self, keys, progress=None):
        """Commit pending keys in insertion order and bounded action-preserving batches."""
        with self.lock:
            rows = [self.ledger.row("operations", key) for key in keys]
            rows = [row for row in rows if row and row["status"] == "PENDING"]
            size = int(self.cfg["write"]["batch_size"])
            report(progress, "Embedding and writing pending records", 0, len(rows))
            # Preserve operation order, including delete followed by re-add.
            for start in range(0, len(rows), size):
                self.check_active()
                group = rows[start : start + size]
                self._commit_group(group)
                report(
                    progress, "Embedding and writing pending records", start + len(group), len(rows)
                )

    def _commit_group(self, rows):
        # Mixed action groups are split without reordering.
        """Split contiguous action groups without reordering delete/re-add operations."""
        start = 0
        while start < len(rows):
            action = rows[start]["action"]
            end = start + 1
            while end < len(rows) and rows[end]["action"] == action:
                end += 1
            group = rows[start:end]
            self._commit_action(action, group)
            start = end

    def _commit_action(self, action, group):
        """Retry one homogeneous action group and acknowledge it after confirmation."""

        def apply():
            """Apply a batch and record throughput after successful upsert."""
            self.check_active()
            started = time.monotonic()
            if action == "DELETE":
                self.store.delete([r["point_id"] for r in group])
            else:
                self.store.upsert([(r["point_id"], json.loads(r["payload_json"])) for r in group])
                self.ledger.measure(
                    "upsert_points_per_second", len(group) / max(time.monotonic() - started, 1e-9)
                )

        def failed(exc):
            """Record one sanitized failure per affected operation and attempt."""
            if isinstance(exc, WorkDeferred):
                return
            for row in group:
                self.ledger.failure("operations", row["idempotency_key"], exc)

        try:
            run_with_retry(apply, self.cfg["write"], failed)
        except WorkDeferred:
            raise
        except Exception as exc:
            for row in group:
                self.ledger.failure(
                    "operations", row["idempotency_key"], exc, terminal=True, count_attempt=False
                )
            raise
        for row in group:
            # Qdrant wait=True must succeed before replayable ledger work is acknowledged.
            self.ledger.finish("operations", row["idempotency_key"])

    def recover(self):
        """Replay work independently so one failure does not block the backlog."""
        failures = 0
        for operation in self.ledger.rows("operations"):
            if self.stop_requested():
                return failures
            try:
                self.commit([operation["idempotency_key"]])
            except Exception:
                failures += 1
        for event in self.ledger.rows("events"):
            if self.stop_requested():
                return failures
            try:
                self.process_event(event["event_id"])
            except Exception:
                failures += 1
        return failures

    def add(self, text, scope, source="manual_tool", session_id="", **fields):
        """Add or reconcile a scoped fact through hash and factual relation review."""
        with self.lock:
            action, identifier, relation = decide(
                text, scope, self.store, self.extractor, self.cfg["dedupe"]
            )
            self.decisions[action] += 1
            self.ledger.increment(action)
            if action == "SKIP":
                return {"action": action, "id": identifier}
            identifier = identifier or point_id(scope, source, content_hash(text))
            metadata = {**fields.pop("metadata", {}), **relation}
            old = self.store.get(identifier, scope)
            value = payload(
                text,
                scope,
                source,
                session_id,
                metadata=metadata,
                created_at=old.payload["created_at"] if old else None,
                **fields,
            )
            key = self.operation(identifier, "UPSERT", value)
            self.commit([key])
            return {"action": action, "id": identifier}

    def process_event(self, key):
        """Resume extraction/preparation/commit phases for one durable event."""
        with self.lock:
            row = self.ledger.row("events", key)
            if not row or row["status"] != "PENDING":
                return
            event = json.loads(row["payload_json"])
            event.setdefault("event_id", key)
            event.setdefault("created_at", row["created_at"])
            try:
                if "operation_keys" not in event:
                    keys = self._prepare(event)
                    event["operation_keys"] = keys
                    self.ledger.prepare_event(key, event)
                self.commit(event["operation_keys"])
                statuses = [
                    self.ledger.row("operations", k)["status"] for k in event["operation_keys"]
                ]
                if any(s not in {"COMMITTED", "SUPERSEDED"} for s in statuses):
                    raise RuntimeError("Event has uncommitted operations")
                self.ledger.finish("events", key)
            except WorkDeferred:
                raise
            except Exception as exc:
                self.ledger.failure("events", key, exc, terminal=True)
                raise

    def _prepare(self, event):
        """Persist extraction candidates and prepare scoped event operations."""
        self.check_active()
        scope = Scope(**event["scope"])
        if event["kind"] == "builtin":
            return self._prepare_builtin(event, scope)
        if event["kind"] == "checkpoint" and not event["extract"]:
            return []
        if "candidates" not in event:
            started = time.monotonic()
            event["candidates"] = run_with_retry(
                lambda: self.extractor.extract(event), self.cfg["write"]
            )
            self.ledger.measure("extract_llm_latency_ms", (time.monotonic() - started) * 1000)
            # Persist extraction before dedupe or remote writes.
            self.ledger.prepare_event(event["event_id"], event)
        keys = []
        for candidate in event["candidates"]:
            self.check_active()
            action, identifier, relation = decide(
                candidate["text"], scope, self.store, self.extractor, self.cfg["dedupe"]
            )
            self.decisions[action] += 1
            self.ledger.increment(action)
            if action == "SKIP":
                continue
            candidate_hash = content_hash(candidate["text"])
            identifier = identifier or point_id(scope, event["source"], candidate_hash)
            if self.ledger.is_delete_fenced(
                identifier, scope, event.get("created_at"), candidate_hash
            ):
                continue
            old = self.store.get(identifier, scope)
            value = payload(
                candidate["text"],
                scope,
                event["source"],
                event["session_id"],
                category=candidate.get("category"),
                importance=candidate.get("importance", 0.5),
                created_at=old.payload["created_at"] if old else event["created_at"],
                updated_at=event["created_at"],
                metadata={"write_origin": event["kind"], **relation},
            )
            keys.append(self.operation(identifier, "UPSERT", value))
        return keys

    def _prepare_builtin(self, event, scope):
        """Mirror builtin changes using authoritative previous content and exact targets."""
        previous = event["metadata"].get("previous_content")
        previous_hash = content_hash(previous) if previous is not None else ""
        if event["action"] in {"replace", "remove"} and previous is None:
            return []
        old_id = (
            point_id(scope, "builtin_memory", [event["target"], previous_hash])
            if previous is not None
            else None
        )
        if event["action"] == "remove":
            return [
                self.operation(
                    old_id,
                    "DELETE",
                    {"content_hash": previous_hash},
                    source_id=self.ledger.delete_fence_source(scope),
                    source_version=event.get("created_at", ""),
                )
            ]
        new_hash = content_hash(event["content"])
        identifier = point_id(scope, "builtin_memory", [event["target"], new_hash])
        if (
            old_id
            and self.ledger.is_delete_fenced(old_id, scope, event.get("created_at"), previous_hash)
        ) or self.ledger.is_delete_fenced(identifier, scope, event.get("created_at"), new_hash):
            return []
        value = payload(
            event["content"],
            scope,
            "builtin_memory",
            event["session_id"],
            metadata={"target": event["target"], "write_origin": "on_memory_write"},
            created_at=event["created_at"],
            updated_at=event["created_at"],
        )
        keys = []
        if old_id and old_id != identifier:
            keys.append(
                self.operation(
                    old_id,
                    "DELETE",
                    {"content_hash": previous_hash},
                    source_id=self.ledger.delete_fence_source(scope),
                    source_version=event.get("created_at", ""),
                )
            )
        keys.append(self.operation(identifier, "UPSERT", value))
        return keys
