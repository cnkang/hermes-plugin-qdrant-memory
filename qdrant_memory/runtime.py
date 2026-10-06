"""Serialized durable mutation engine, shared by provider and migration CLI."""
import json
import time
from threading import RLock
from .models import Scope, content_hash, enforce_limits, payload, point_id, now
from .retry import run_with_retry
from .dedupe import decide


class Runtime:
    def __init__(self, cfg, store, ledger, extractor=None):
        self.cfg, self.store, self.ledger, self.extractor = cfg, store, ledger, extractor
        self.lock = RLock()
        self.decisions = {"ADD": 0, "UPDATE": 0, "SKIP": 0}
        self.store.metrics = ledger

    def operation(self, identifier, action, value, source_id="", source_version=""):
        if action == "DELETE" and not source_version:
            source_version = now()
        if action != "DELETE":
            value = enforce_limits(value, self.cfg["limits"])
        return self.ledger.enqueue_operation(identifier, action, value, source_id, source_version)

    def commit(self, keys):
        with self.lock:
            rows = [self.ledger.row("operations", key) for key in keys]
            rows = [row for row in rows if row and row["status"] == "PENDING"]
            size = int(self.cfg["write"]["batch_size"])
            # Preserve operation order, including delete followed by re-add.
            for start in range(0, len(rows), size):
                group = rows[start:start + size]
                self._commit_group(group)

    def _commit_group(self, rows):
        # Mixed action groups are split without reordering.
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
        def apply():
            started = time.monotonic()
            if action == "DELETE":
                self.store.delete([r["point_id"] for r in group])
            else:
                self.store.upsert([(r["point_id"], json.loads(r["payload_json"])) for r in group])
                self.ledger.measure("upsert_points_per_second", len(group) / max(time.monotonic() - started, 1e-9))
        def failed(exc):
            for row in group:
                self.ledger.failure("operations", row["idempotency_key"], exc)
        try:
            run_with_retry(apply, self.cfg["write"], failed)
        except Exception as exc:
            for row in group:
                self.ledger.failure("operations", row["idempotency_key"], exc, terminal=True, count_attempt=False)
            raise
        for row in group:
            self.ledger.finish("operations", row["idempotency_key"])

    def recover(self):
        failures = 0
        for operation in self.ledger.rows("operations"):
            try:
                self.commit([operation["idempotency_key"]])
            except Exception:
                failures += 1
        for event in self.ledger.rows("events"):
            try:
                self.process_event(event["event_id"])
            except Exception:
                failures += 1
        return failures

    def add(self, text, scope, source="manual_tool", session_id="", **fields):
        with self.lock:
            action, identifier, relation = decide(text, scope, self.store, self.extractor, self.cfg["dedupe"])
            self.decisions[action] += 1
            if action == "SKIP":
                return {"action": action, "id": identifier}
            identifier = identifier or point_id(scope, source, content_hash(text))
            metadata = {**fields.pop("metadata", {}), **relation}
            old = self.store.get(identifier, scope)
            value = payload(text, scope, source, session_id, metadata=metadata,
                            created_at=old.payload["created_at"] if old else None, **fields)
            key = self.operation(identifier, "UPSERT", value)
            self.commit([key])
            return {"action": action, "id": identifier}

    def process_event(self, key):
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
                statuses = [self.ledger.row("operations", k)["status"] for k in event["operation_keys"]]
                if any(s not in {"COMMITTED", "SUPERSEDED"} for s in statuses):
                    raise RuntimeError("Event has uncommitted operations")
                self.ledger.finish("events", key)
            except Exception as exc:
                self.ledger.failure("events", key, exc, terminal=True)
                raise

    def _prepare(self, event):
        scope = Scope(**event["scope"])
        if event["kind"] == "builtin":
            return self._prepare_builtin(event, scope)
        if "candidates" not in event:
            started = time.monotonic()
            event["candidates"] = run_with_retry(lambda: self.extractor.extract(event), self.cfg["write"])
            self.ledger.measure("extract_llm_latency_ms", (time.monotonic() - started) * 1000)
            # Persist extraction before dedupe or remote writes.
            self.ledger.prepare_event(event["event_id"], event)
        keys = []
        for candidate in event["candidates"]:
            action, identifier, relation = decide(candidate["text"], scope, self.store, self.extractor, self.cfg["dedupe"])
            self.decisions[action] += 1
            if action == "SKIP":
                continue
            identifier = identifier or point_id(scope, event["source"], content_hash(candidate["text"]))
            old = self.store.get(identifier, scope)
            value = payload(candidate["text"], scope, event["source"], event["session_id"],
                            category=candidate.get("category"), importance=candidate.get("importance", 0.5),
                            created_at=old.payload["created_at"] if old else event["created_at"],
                            updated_at=event["created_at"], metadata={"write_origin": event["kind"], **relation})
            keys.append(self.operation(identifier, "UPSERT", value))
        return keys

    def _prepare_builtin(self, event, scope):
        previous = event["metadata"].get("previous_content")
        if event["action"] in {"replace", "remove"} and previous is None:
            return []
        old_id = point_id(scope, "builtin_memory", [event["target"], content_hash(previous)]) if previous is not None else None
        if event["action"] == "remove":
            return [self.operation(old_id, "DELETE", {})]
        identifier = point_id(scope, "builtin_memory", [event["target"], content_hash(event["content"])])
        value = payload(event["content"], scope, "builtin_memory", event["session_id"],
                        metadata={"target": event["target"], "write_origin": "on_memory_write"},
                        created_at=event["created_at"], updated_at=event["created_at"])
        keys = []
        if old_id and old_id != identifier:
            keys.append(self.operation(old_id, "DELETE", {}))
        keys.append(self.operation(identifier, "UPSERT", value))
        return keys
