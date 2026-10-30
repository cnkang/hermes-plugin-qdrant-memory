"""Generate the soak workload and record every acknowledged outcome.

The generator drives the real remote client against the real Gateway. It owns
the caller half of the retry contract: a retryable failure is replayed with the
original ``request_id`` or ``event_id`` until the durable-outbox deadline. A
final outcome is written once per logical operation, so the verifier can compare
acknowledged state with the ledger and with Qdrant.
"""

import asyncio
import itertools
import random
import time
from dataclasses import dataclass

from .common import utc_now

TOPICS = (
    "ledger",
    "gateway",
    "restart",
    "retrieval",
    "quarantine",
    "lease",
    "fencing",
    "tls",
    "dedupe",
    "outbox",
)
DETAILS = (
    "keeps the write-ahead journal",
    "replays the original identifier",
    "holds the writer lease across restarts",
    "answers from the stored receipt",
    "keeps private rows inside one partition",
    "rejects a stale revision",
)
WRITE_TOOLS = ("add", "update", "delete")


@dataclass
class Outcome:
    """One logical operation's final, evidence-bearing result."""

    ok: bool
    value: dict
    error: dict
    attempts: int
    replay_count: int
    duration_ms: float
    resolved: bool

    @property
    def action(self):
        """Return the Owner action, or an empty string for a rejection."""
        return self.value.get("action", "") if isinstance(self.value, dict) else ""


class SoakClient:
    """Call the Owner through the production remote client with a durable outbox."""

    def __init__(
        self,
        *,
        endpoint,
        ca_file,
        tokens,
        journal,
        outbox_seconds=150.0,
        backoff_cap=5.0,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ):
        """Bind one endpoint, the credential set and the evidence stream."""
        self.endpoint = endpoint
        self.ca_file = ca_file
        self.tokens = tokens
        self.journal = journal
        self.outbox_seconds = outbox_seconds
        self.backoff_cap = backoff_cap
        self.clock = clock
        self.sleep = sleep
        # The Runner pins the end of the window so a Retry-After wait cannot
        # stretch the measured duration far past the requested window.
        self.deadline = None
        # The Runner reports whether a declared client-path fault is open. A
        # handshake cut then never reached the Owner, so the caller may retry it.
        self.path_fault = None

    async def _attempt(self, identity, coroutine):
        """Run one attempt, keeping a stable machine failure description."""
        from qdrant_memory.remote_client import remote_error

        started = self.clock()
        try:
            value = await coroutine
        except Exception as exc:  # noqa: BLE001 - every transport failure is data here
            return None, remote_error(exc, replay="request_id"), self.clock() - started
        error = value.get("error") if isinstance(value, dict) else None
        if isinstance(error, dict):
            failure = {
                "type": error.get("type", "OwnerError"),
                "reason": error.get("reason") or "business_error",
                "retryable": error.get("retryable") is True,
            }
            if error.get("retry_after") is not None:
                failure["retry_after"] = error["retry_after"]
            return None, failure, self.clock() - started
        return value, None, self.clock() - started

    async def call(
        self,
        identity,
        tool,
        args,
        *,
        op,
        request_id,
        text=None,
        phase="steady",
        replay=True,
        outbox=None,
        seq=None,
    ):
        """Run one idempotent tool operation, replaying only retryable failures."""
        from qdrant_memory.remote_client import call_remote

        name = tool if tool.startswith("qdrant_memory_") else "qdrant_memory_" + tool
        # Every shared mutation carries its identifier in the arguments; the
        # schema rejects a write without one. Reads carry none by contract.
        arguments = dict(args) if name.endswith("search") else {**args, "request_id": request_id}
        token = self.tokens[identity]
        deadline = self.clock() + (outbox or self.outbox_seconds)
        if self.deadline is not None and outbox is None:
            deadline = min(deadline, self.deadline)
        attempts = 0
        replay_count = 0
        started = self.clock()
        while True:
            attempts += 1
            value, failure, elapsed = await self._attempt(
                identity,
                call_remote(self.endpoint, token, name, arguments, ca_file=self.ca_file),
            )
            self.journal.record(
                "attempt",
                op=op,
                tool=tool,
                identity=identity,
                request_id=request_id,
                attempt=attempts,
                phase=phase,
                duration_ms=round(elapsed * 1000, 3),
                error=failure,
            )
            if failure is None:
                break
            if (
                failure.get("retryable") is not True
                and failure.get("reason") in {"tls_failed", "unreachable", "outcome_unknown"}
                and self.path_fault is not None
                and self.path_fault()
            ):
                # The path is deliberately cut, so this attempt never reached the
                # Owner. Hold the identifier and try again while the cut lasts.
                failure = {**failure, "retryable": True, "path_outage": True}
            remaining = deadline - self.clock()
            if not replay or failure.get("retryable") is not True or remaining <= 0:
                break
            replay_count += 1
            delay = min(self.backoff_cap, 0.1 * 2 ** min(replay_count, 6))
            retry_after = failure.get("retry_after")
            if isinstance(retry_after, (int, float)):
                # A throttled Owner asked for a real delay; honouring it keeps
                # the caller from turning one window into a retry storm.
                delay = max(delay, retry_after)
            await self.sleep(min(delay, max(remaining, 0)))
        duration = round((self.clock() - started) * 1000, 3)
        outcome = Outcome(
            ok=failure is None,
            value=value or {},
            error=failure or {},
            attempts=attempts,
            replay_count=replay_count,
            duration_ms=duration,
            resolved=failure is None or failure.get("retryable") is not True,
        )
        record = {
            "op": op,
            "tool": tool,
            "identity": identity,
            "request_id": request_id,
            "phase": phase,
            "attempts": attempts,
            "replays": replay_count,
            "duration_ms": duration,
            "result": "ok" if outcome.ok else "error",
            "error": failure,
            "action": outcome.action,
            "record_id": outcome.value.get("id") if isinstance(outcome.value, dict) else None,
            "version": outcome.value.get("version") if isinstance(outcome.value, dict) else None,
            "args": arguments,
            "seq": seq,
        }
        if text is not None:
            record["text"] = text
        self.journal.record("op", **record)
        return outcome

    async def submit_event(self, identity, event, *, phase="steady", outbox=None, seq=None):
        """Submit one lifecycle turn, replaying the same event ID on a transient failure."""
        from qdrant_memory.remote_client import send_event

        token = self.tokens[identity]
        deadline = self.clock() + (outbox or self.outbox_seconds)
        if self.deadline is not None and outbox is None:
            deadline = min(deadline, self.deadline)
        attempts = replay_count = 0
        started = self.clock()
        while True:
            attempts += 1
            value, failure, elapsed = await self._attempt(
                identity,
                send_event(self.endpoint, token, event, ca_file=self.ca_file),
            )
            self.journal.record(
                "attempt",
                op="event",
                tool="event",
                identity=identity,
                event_id=event["event_id"],
                attempt=attempts,
                phase=phase,
                duration_ms=round(elapsed * 1000, 3),
                error=failure,
            )
            if failure is None:
                break
            remaining = deadline - self.clock()
            if failure.get("retryable") is not True or remaining <= 0:
                break
            replay_count += 1
            delay = min(self.backoff_cap, 0.1 * 2 ** min(replay_count, 6))
            retry_after = failure.get("retry_after")
            if isinstance(retry_after, (int, float)):
                delay = max(delay, retry_after)
            await self.sleep(min(delay, max(deadline - self.clock(), 0)))
        duration = round((self.clock() - started) * 1000, 3)
        status = (value or {}).get("status")
        # A non-committed status is a terminal outcome of its own, never a
        # missing error field: downstream readers must see a mapping.
        error = failure
        if status != "COMMITTED" and failure is None:
            error = {"type": "LifecycleStatus", "status": status, "retryable": False}
        outcome = Outcome(
            ok=failure is None and status == "COMMITTED",
            value=value or {},
            error=error,
            attempts=attempts,
            replay_count=replay_count,
            duration_ms=duration,
            resolved=failure is None,
        )
        self.journal.record(
            "op",
            op="event",
            tool="event",
            identity=identity,
            event_id=event["event_id"],
            event_text=event["user"],
            phase=phase,
            attempts=attempts,
            replays=replay_count,
            duration_ms=duration,
            result="ok" if outcome.ok else "error",
            status=status,
            error=error,
            seq=seq,
        )
        return outcome

    async def abandon_response(self, identity, tool, args, *, request_id):
        """Send a real write and close without reading the response.

        The Owner commits, then the client loses the answer. The replay with the
        same identifier must return the stored receipt instead of a second row.
        """
        import ssl

        import httpx

        name = tool if tool.startswith("qdrant_memory_") else "qdrant_memory_" + tool
        # httpx treats a bare path as an SSL context, so build the context here.
        context = ssl.create_default_context(cafile=str(self.ca_file))
        async with httpx.AsyncClient(
            verify=context, trust_env=False, timeout=30, follow_redirects=False
        ) as http:
            async with http.stream(
                "POST",
                self.endpoint,
                headers={
                    "Authorization": "Bearer " + self.tokens[identity],
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-11-25",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 101,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": {**args, "request_id": request_id}},
                },
            ) as response:
                status = response.status_code
                content_type = response.headers.get("content-type", "")
        self.journal.record(
            "lost_response",
            tool=tool,
            identity=identity,
            request_id=request_id,
            status=status,
            content_type=content_type,
            phase="network",
        )
        return {"status": status, "content_type": content_type}


def record_text(marker, topic, detail):
    """Compose one synthetic fact that carries its unique marker."""
    return f"Fact {marker}: the {topic} path {detail}."


def record_query(marker, topic):
    """Compose a retrieval query that shares the marker and topic tokens."""
    return f"{marker} {topic} path"


class Workload:
    """Drive a deterministic operation mix and keep the caller-side record list."""

    def __init__(self, client, journal, *, seed, lifecycle=True, start_index=0):
        """Bind the client, the evidence stream and the deterministic seed."""
        self.client = client
        self.journal = journal
        self.random = random.Random(seed)
        self.lifecycle = lifecycle
        self.worker_state = {}
        self.records = {}
        self.start_index = start_index
        self.sequence = itertools.count(1)

    def _writer(self, index, offset=0):
        """Rotate the project writers so no single credential saturates its window.

        The offset is the worker's own position. Without it every worker picks the
        same writer at the same index, and the three of them burst one credential
        at once, which trips the Gateway's per-principal window.
        """
        writers = ("hermes-a", "hermes-b", "hermes-c", "codex", "codex-b")
        return writers[(index + offset) % len(writers)]

    @staticmethod
    def _offset(worker):
        """Return a stable per-worker rotation offset."""
        digits = "".join(character for character in worker if character.isdigit())
        return int(digits) if digits else 0

    def _state(self, worker):
        """Return the per-worker mutable state."""
        return self.worker_state.setdefault(worker, {"index": 0, "owned": [], "disposable": []})

    def _marker(self, worker, index):
        """Build a unique marker with a random suffix, never a bare sequence."""
        suffix = "".join(
            self.random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(8)
        )
        return f"SOAK-{worker}-{index}-{suffix}"

    async def step(self, worker, phase="steady"):
        """Run one operation and record its acknowledged result."""
        state = self._state(worker)
        index = state["index"]
        state["index"] += 1
        rotation = (index + self.start_index) % 10
        if rotation < 5:
            return await self._add(worker, index, phase)
        if rotation < 7:
            return await self._search(worker, index, phase)
        if rotation == 7:
            return await self._update(worker, index, phase)
        if rotation == 8:
            return await self._delete(worker, index, phase)
        if self.lifecycle:
            return await self._event(worker, index, phase)
        return await self._search(worker, index, phase)

    def _next_seq(self):
        """Return the next logical order for one operation."""
        return next(self.sequence)

    async def _add(self, worker, index, phase):
        """Add a unique fact and verify exact dedupe on a second identifier."""
        marker = self._marker(worker, index)
        topic = TOPICS[index % len(TOPICS)]
        detail = DETAILS[index % len(DETAILS)]
        text = record_text(marker, topic, detail)
        request_id = f"{worker}-add-{index}"
        outcome = await self.client.call(
            self._writer(index, self._offset(worker)),
            "add",
            {"text": text},
            op="add",
            request_id=request_id,
            text=text,
            phase=phase,
            seq=self._next_seq(),
        )
        if outcome.ok:
            self.records[marker] = {
                "id": outcome.value.get("id"),
                "version": outcome.value.get("version"),
                "text": text,
                "topic": topic,
                "identity": self._writer(index, self._offset(worker)),
                "worker": worker,
                "action": outcome.action,
            }
            self._state(worker)["owned"].append(marker)
            if len(self._state(worker)["disposable"]) < 2:
                await self._add_disposable(worker, index, phase)
        return outcome

    async def _add_disposable(self, worker, index, phase):
        """Create a record that a later step deletes."""
        marker = self._marker(worker, index) + "-disposable"
        text = record_text(marker, "disposable", "is removed by the delete path")
        request_id = f"{worker}-disposable-{index}"
        outcome = await self.client.call(
            "hermes-b",
            "add",
            {"text": text},
            op="add",
            request_id=request_id,
            text=text,
            phase=phase,
            seq=self._next_seq(),
        )
        if outcome.ok:
            self.records[marker] = {
                "id": outcome.value.get("id"),
                "version": outcome.value.get("version"),
                "text": text,
                "topic": "disposable",
                "identity": "hermes-b",
                "worker": worker,
                "action": outcome.action,
            }
            self._state(worker)["disposable"].append(marker)
        return outcome

    async def _search(self, worker, index, phase):
        """Query for a known record and measure retrieval separately from loss."""
        owned = self._state(worker)["owned"]
        if not owned:
            return await self._add(worker, index, phase)
        marker = owned[index % len(owned)]
        record = self.records[marker]
        outcome = await self.client.call(
            self._writer(index, self._offset(worker)),
            "search",
            {"query": record_query(marker, record["topic"])},
            op="search",
            request_id=f"{worker}-search-{index}",
            phase=phase,
            seq=self._next_seq(),
        )
        if outcome.ok:
            found = {
                item.get("id")
                for item in outcome.value.get("memories", [])
                if isinstance(item, dict)
            }
            self.journal.record(
                "retrieval",
                identity=self._writer(index, self._offset(worker)),
                marker=marker,
                record_id=record["id"],
                hit=record["id"] in found,
                phase=phase,
            )
        return outcome

    async def _update(self, worker, index, phase):
        """Update an owned record with its current revision."""
        owned = self._state(worker)["owned"]
        if not owned:
            return await self._add(worker, index, phase)
        marker = owned[index % len(owned)]
        record = self.records[marker]
        revision = index
        text = record["text"] + f" Revision {revision} keeps the same subject."
        outcome = await self.client.call(
            self._writer(index + 1, self._offset(worker)),
            "update",
            {"id": record["id"], "text": text, "expected_version": record["version"]},
            op="update",
            request_id=f"{worker}-update-{index}",
            text=text,
            phase=phase,
            seq=self._next_seq(),
        )
        if outcome.ok:
            record.update(version=outcome.value.get("version"), text=text)
        return outcome

    async def _delete(self, worker, index, phase):
        """Delete one disposable record, keeping its exact revision."""
        state = self._state(worker)
        if not state["disposable"]:
            return await self._add_disposable(worker, index, phase)
        marker = state["disposable"].pop(0)
        record = self.records[marker]
        outcome = await self.client.call(
            self._writer(index + 2, self._offset(worker)),
            "delete",
            {"id": record["id"], "expected_version": record["version"]},
            op="delete",
            request_id=f"{worker}-delete-{index}",
            text=record["text"],
            phase=phase,
            seq=self._next_seq(),
        )
        if outcome.ok:
            record["deleted"] = True
            record["version"] = outcome.value.get("version")
        return outcome

    async def _event(self, worker, index, phase):
        """Submit one lifecycle turn through the authenticated event route."""
        marker = self._marker(worker, index) + "-turn"
        text = record_text(marker, "lifecycle", "captures one user turn durably")
        event = {
            "event_id": f"{worker}-event-{index}",
            "session_id": f"soak-session-{worker}",
            "kind": "turn_completed",
            "user": text,
            "assistant": "Noted.",
        }
        writer = self._writer(index, self._offset(worker))
        event["session_id"] = f"soak-session-{writer}"
        outcome = await self.client.submit_event(writer, event, phase=phase, seq=self._next_seq())
        if outcome.ok:
            self.records[marker] = {
                "id": None,
                "version": None,
                "text": text,
                "topic": "lifecycle",
                "identity": writer,
                "worker": worker,
                "action": "EVENT",
            }
        return outcome

    async def run(
        self, *, stop_at, workers=4, phase="steady", clock=time.monotonic, should_stop=None
    ):
        """Run every worker until the monotonic deadline passes."""

        async def loop(worker):
            while clock() < stop_at and not (should_stop and should_stop()):
                try:
                    outcome = await self.step(worker, phase)
                    if (getattr(outcome, "error", None) or {}).get("path_outage"):
                        # The path is cut; back off instead of creating new work.
                        await asyncio.sleep(0.5)
                except Exception as exc:  # noqa: BLE001 - harness must keep evidence
                    self.journal.record(
                        "harness",
                        event="workload_step_error",
                        worker=worker,
                        error={"type": type(exc).__name__},
                    )
                    await asyncio.sleep(0.2)

        await asyncio.gather(*(loop(f"w{number}") for number in range(workers)))
        return {"workers": workers, "records": len(self.records)}

    def pending_writes(self, rows):
        """Return the logical operations that never reached an acknowledgement."""
        acknowledged = {
            row.get("request_id")
            for row in rows
            if row.get("kind") == "op" and row.get("result") == "ok"
        }
        pending = {}
        for row in rows:
            if row.get("kind") != "op" or row.get("result") == "ok":
                continue
            if row.get("tool") not in {"add", "update", "delete", "event"}:
                continue
            request_id = row.get("request_id") or row.get("event_id")
            if request_id in acknowledged:
                continue
            if (row.get("error") or {}).get("retryable") is not True:
                continue
            pending[request_id] = row
        return pending

    async def flush_pending(self, client, journal):
        """Replay every unresolved identifier once the faults have stopped.

        This is the caller's durable-outbox drain. It deliberately outlives the
        load window and may wait out a Retry-After, because the question here is
        whether the Owner can still answer for the original identifier.
        """
        client.deadline = None
        pending = self.pending_writes(journal.rows())
        results = {"attempted": len(pending), "recovered": 0, "failed": 0}
        for request_id, row in pending.items():
            if row.get("tool") == "event":
                event = {
                    "event_id": request_id,
                    "session_id": f"soak-session-{row.get('identity', 'hermes-a')}",
                    "kind": "turn_completed",
                    "user": row.get("event_text", ""),
                    "assistant": "Noted.",
                }
                outcome = await client.submit_event(
                    row.get("identity", "hermes-a"),
                    event,
                    phase="recovery",
                    outbox=300.0,
                    seq=row.get("seq"),
                )
                results["recovered" if outcome.ok else "failed"] += 1
                continue
            outcome = await client.call(
                row.get("identity", "hermes-a"),
                row.get("tool", "add"),
                row.get("args", {}),
                # Keep the original class: a replayed probe must stay a probe.
                op=row.get("op") or row.get("tool", "add"),
                request_id=request_id,
                text=row.get("text"),
                phase="recovery",
                outbox=300.0,
                seq=row.get("seq"),
            )
            results["recovered" if outcome.ok else "failed"] += 1
        journal.record("recovery", **results)
        return results


async def probe_authorization(client, journal):
    """Prove that a read-only credential cannot write and that spaces stay apart."""
    marker = f"SOAK-probe-{utc_now().replace(':', '').replace('.', '')}"
    private_text = f"Fact {marker}-private: the private partition keeps its own rows."
    private = await client.call(
        "hermes-private",
        "add",
        {"text": private_text},
        op="probe",
        request_id=f"probe-private-add-{marker}",
        text=private_text,
    )
    denied = await client.call(
        "reader",
        "add",
        {"text": f"Fact {marker}-denied: a read-only credential must not write."},
        op="probe",
        request_id=f"probe-readonly-add-{marker}",
    )
    foreign = await client.call(
        "codex-private",
        "search",
        {"query": f"{marker}-private private partition"},
        op="probe",
        request_id=f"probe-private-search-{marker}",
    )
    project = await client.call(
        "codex",
        "search",
        {"query": f"{marker}-private private partition"},
        op="probe",
        request_id=f"probe-project-search-{marker}",
    )
    leaked_ids = set()
    for outcome in (foreign, project):
        for item in outcome.value.get("memories", []) if outcome.ok else []:
            if isinstance(item, dict) and marker in str(item.get("text", "")):
                leaked_ids.add(item.get("id"))
    results = {
        "private_write": private.ok,
        "readonly_denied": (not denied.ok) and denied.error.get("type") == "PermissionError",
        "cross_scope_hidden": not leaked_ids,
        "private_record": private.value.get("id") if private.ok else None,
    }
    checks = [private.ok, results["readonly_denied"], results["cross_scope_hidden"]]
    status = "PASS" if all(checks) else "FAIL"
    journal.record("probe", probe="authorization", status=status, marker=marker, **results)
    return {"status": status, **results}


async def probe_cas(client, journal):
    """Prove that two concurrent revisions cannot both win.

    The probe owns two dedicated devices (``cas-a`` and ``cas-b``), so workload
    traffic can never consume their request budget mid-race. A round that is
    throttled says nothing about compare-and-swap, so it is retried with a new
    record instead of being reported as a product failure.
    """
    marker = text = ""
    for _ in range(3):
        marker = utc_now().replace(":", "").replace(".", "")
        text = f"Fact SOAK-cas-{marker}: the ledger accepts one revision."
        saved = await client.call(
            "cas-a",
            "add",
            {"text": text},
            op="probe",
            request_id=f"probe-cas-add-{marker}",
            text=text,
        )
        if saved.ok:
            break
    else:
        journal.record("probe", probe="cas", status="INCOMPLETE", detail="add failed three times")
        return {"status": "INCOMPLETE"}
    base = {
        "id": saved.value["id"],
        "expected_version": saved.value["version"],
        "text": text + " v2",
    }
    first, second = await asyncio.gather(
        client.call("cas-a", "update", {**base}, op="probe", request_id=f"probe-cas-a-{marker}"),
        client.call("cas-b", "update", {**base}, op="probe", request_id=f"probe-cas-b-{marker}"),
    )
    updates = sum(1 for outcome in (first, second) if outcome.ok and outcome.action == "UPDATE")
    conflicts = sum(
        1
        for outcome in (first, second)
        if not outcome.ok and outcome.error.get("type") == "SharedConflictError"
    )
    throttled = any(outcome.error.get("retryable") is True for outcome in (first, second))
    if throttled and (updates, conflicts) != (1, 1):
        journal.record("probe", probe="cas", status="INCOMPLETE", detail="throttled round")
        return {"status": "INCOMPLETE"}
    status = "PASS" if (updates, conflicts) == (1, 1) else "FAIL"
    journal.record(
        "probe",
        probe="cas",
        status=status,
        updates=updates,
        conflicts=conflicts,
        record_id=saved.value.get("id"),
        version=next(
            (outcome.value.get("version") for outcome in (first, second) if outcome.ok), None
        ),
        text=text + " v2" if updates else text,
    )
    return {"status": status, "updates": updates, "conflicts": conflicts}


async def probe_retry_contract(client, journal, *, tokens, endpoint, ca_file, untrusted_ca=None):
    """Prove the terminal and retryable classifications the ADR defines."""
    from qdrant_memory.remote_client import call_remote, remote_error

    results = {}

    async def classify(coroutine):
        try:
            await coroutine
        except Exception as exc:  # noqa: BLE001 - the classification is the assertion
            return remote_error(exc, replay="request_id")
        return {}

    # A credential the Owner does not know is final, never replayed.
    unknown = "z" * 48
    results["authentication_rejected"] = await classify(
        call_remote(endpoint, unknown, "qdrant_memory_search", {"query": "x"}, ca_file=ca_file)
    )
    # An untrusted CA is a local trust failure, not a transport retry.
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as handle:
        handle.write("-----BEGIN CERTIFICATE-----\nnot a certificate\n-----END CERTIFICATE-----\n")
        broken = handle.name
    results["tls_verification_failed"] = await classify(
        call_remote(
            endpoint, tokens["hermes-a"], "qdrant_memory_search", {"query": "x"}, ca_file=broken
        )
    )
    statuses = {
        "authentication_rejected": results["authentication_rejected"].get("reason")
        == "authentication_rejected"
        and results["authentication_rejected"].get("retryable") is False,
        "tls_verification_failed": results["tls_verification_failed"].get("reason")
        in {"tls_verification_failed", "tls_failed"}
        and results["tls_verification_failed"].get("retryable") is False,
    }
    journal.record(
        "probe",
        probe="retry_contract",
        status="PASS" if all(statuses.values()) else "FAIL",
        reasons={key: results[key].get("reason") for key in results},
        detail={key: bool(value) for key, value in statuses.items()},
    )
    return {"status": "PASS" if all(statuses.values()) else "FAIL", **statuses}


async def probe_rate_limit(client, journal, *, rounds=320):
    """Drive one dedicated credential past the gateway window and read the answer."""
    rejected = []
    for index in range(rounds):
        outcome = await client.call(
            "burst",
            "search",
            {"query": f"rate window probe {index}"},
            op="probe",
            request_id=f"probe-rate-{index}",
            replay=False,
        )
        if not outcome.ok:
            rejected.append(outcome.error)
        if len(rejected) >= 3:
            break
    retryable = [item for item in rejected if item.get("retryable") is True]
    delayed = [item for item in retryable if (item.get("retry_after") or 0) >= 1]
    status = "PASS" if retryable and delayed else "INCOMPLETE"
    journal.record(
        "probe",
        probe="rate_limit",
        status=status,
        rejected=len(rejected),
        retryable=len(retryable),
        with_retry_after=len(delayed),
        reasons=sorted({item.get("reason", "") for item in rejected}),
    )
    return {"status": status, "rejected": len(rejected), "retryable": len(retryable)}
