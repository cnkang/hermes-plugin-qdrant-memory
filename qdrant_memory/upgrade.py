"""Operator-driven, resumable adoption of one Qdrant policy on a destination.

The maintenance window is deliberately short and explicit:

* ``plan`` inspects one destination and persists the plan. It never initializes
  a collection, writes the Ledger, runs recovery, creates an index or changes
  configuration.
* ``apply`` and ``resume`` re-validate that saved plan against the live profile
  before touching anything, then run idempotent steps and record each one.
* ``status`` only reads the saved plan. It never opens the destination.

Every durable write goes through :mod:`qdrant_memory.strategy` so the policy
record and the upgrade checkpoint share one private, atomic publication path.
"""

import json
import os
import shutil
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from . import strategy
from .models import digest, now
from .ownership import WriterLease
from .qdrant_store import IDENTITY_ID, build_client
from .shared_state import pending_shared

PHASE_PLANNED = "planned"
PHASE_APPLYING = "applying"
PHASE_VERIFYING = "verifying"
PHASE_READY = "ready"
PHASE_FAILED = "failed"
LIVE_PHASES = {PHASE_PLANNED, PHASE_APPLYING, PHASE_VERIFYING, PHASE_FAILED}
STEPS = ("backup", "indexes", "verify", "activate")
STATE_SUFFIX = "upgrade"
BACKUP_MARKER = "backup.complete.json"

REMOTE_BACKUP_NOTE = (
    "This Runtime does not hold Qdrant administrative backup rights. Take a "
    "provider-side snapshot at the same stopped-writer point and keep the "
    "attestation with the maintenance record."
)


class UpgradeError(ValueError):
    """Base class for an operator-correctable upgrade refusal."""


class UpgradeStateError(UpgradeError):
    """Require a saved plan before applying or resuming."""


class UpgradePlanStaleError(UpgradeError):
    """Refuse a plan whose destination, profile or settings have moved."""


class UpgradeRefusedError(UpgradeError):
    """Refuse an upgrade that cannot prove isolation or safety."""


class UpgradeBlockedError(UpgradeError):
    """Report blockers that make this destination ineligible for the upgrade."""

    def __init__(self, blockers):
        """Carry the machine-readable blockers alongside the operator message."""
        self.blockers = list(blockers)
        codes = ", ".join(item.get("code", "blocked") for item in self.blockers)
        super().__init__(f"Upgrade is blocked: {codes}")


def _stamp():
    """Return a filesystem-safe UTC timestamp for a recovery point."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _server_version(client, mode):
    """Read the backend version, reporting ``unknown`` instead of guessing."""
    source = "embedded-engine" if mode == "embedded" else "server-info"
    try:
        info = client.info()
    except Exception:
        return {"version": None, "source": "unavailable"}
    version = getattr(info, "version", None)
    if not version:
        return {"version": None, "source": "unavailable"}
    return {"version": str(version), "source": source}


def _strict_mode(client, collection):
    """Report a server strict-mode setting when the backend exposes it."""
    try:
        config = client.get_collection(collection).config
    except Exception:
        return "unknown"
    value = getattr(config, "strict_mode_config", None)
    if value is None:
        return "disabled"
    return "enabled" if getattr(value, "enabled", False) else "disabled"


def _schema(client, collection):
    """Return the collection's named dense vector contract."""
    info = client.get_collection(collection)
    vectors = info.config.params.vectors
    if not isinstance(vectors, dict) or "dense" not in vectors:
        return None
    dense = vectors["dense"]
    return {
        "size": dense.size,
        "distance": str(getattr(dense.distance, "value", dense.distance)),
        "datatype": str(getattr(dense.datatype, "value", dense.datatype or "float32")),
    }


def _identity(client, collection):
    """Read the reserved embedding identity point without modifying it."""
    rows = client.retrieve(collection, ids=[IDENTITY_ID], with_payload=True)
    if not rows:
        return None
    payload = rows[0].payload or {}
    return {
        "embedding_fingerprint": payload.get("embedding_fingerprint"),
        "schema": payload.get("_qdrant_memory_schema"),
    }


def _memory_count(client, collection):
    """Count stored memories, excluding the reserved identity sentinel."""
    from qdrant_client import models as m

    return client.count(
        collection,
        count_filter=m.Filter(must_not=[m.HasIdCondition(has_id=[IDENTITY_ID])]),
        exact=True,
    ).count


def _ledger_snapshot(home, namespace):
    """Read pending work and destructive intents from an existing ledger only."""
    path = Path(home).resolve() / "qdrant-memory" / "state.db"
    snapshot = {
        "present": False,
        "operations": {},
        "events": {},
        "reset_intents": 0,
        "scope_deletes": 0,
        "shared_requests": 0,
        "owner_receipts": 0,
    }
    if not path.is_file():
        return snapshot
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        snapshot["present"] = True
        for table in ("events", "operations"):
            if table in tables:
                snapshot[table] = dict(
                    db.execute(
                        f"SELECT status,COUNT(*) FROM {table} WHERE collection=? GROUP BY status",
                        (namespace,),
                    )
                )
        if "qdrant_reset_intents" in tables:
            snapshot["reset_intents"] = db.execute(
                "SELECT COUNT(*) FROM qdrant_reset_intents WHERE collection=?", (namespace,)
            ).fetchone()[0]
        if "qdrant_scope_deletes" in tables:
            snapshot["scope_deletes"] = db.execute(
                "SELECT COUNT(*) FROM qdrant_scope_deletes WHERE collection=?", (namespace,)
            ).fetchone()[0]
        snapshot.update(pending_shared(db, namespace))
        return snapshot


def _shared_work(snapshot):
    """Report only Shared Owner work that the upgrade interlock must respect."""
    blockers = []
    if snapshot.get("shared_requests", 0):
        blockers.append(
            {
                "code": "shared_pending",
                "detail": "shared requests are still unacknowledged",
            }
        )
    if snapshot.get("owner_receipts", 0):
        blockers.append(
            {
                "code": "shared_owner_receipt",
                "detail": (
                    "the Shared Owner holds an admitted lifecycle receipt whose event "
                    "has not reached a terminal state; complete the Owner recovery path, "
                    "then resume or plan again"
                ),
            }
        )
    return blockers


def _unresolved_work(snapshot):
    """Return blockers for pending work that only a recovery path may resolve."""
    blockers = []
    if snapshot.get("operations", {}).get("PENDING", 0):
        blockers.append({"code": "pending_operations", "detail": "pending writes remain"})
    if snapshot.get("events", {}).get("FAILED", 0):
        blockers.append(
            {
                "code": "failed_events",
                "detail": "a failed lifecycle event needs retry or operator recovery",
            }
        )
    if snapshot.get("events", {}).get("QUARANTINED", 0):
        blockers.append(
            {
                "code": "quarantined_events",
                "detail": "quarantined events need an explicit operator decision",
            }
        )
    if snapshot.get("reset_intents", 0):
        blockers.append(
            {"code": "reset_intent", "detail": "an interrupted reset must be resumed by init"}
        )
    if snapshot.get("scope_deletes", 0):
        blockers.append(
            {
                "code": "scope_delete_intent",
                "detail": "an interrupted scoped deletion must be resumed by delete-all",
            }
        )
    blockers.extend(_shared_work(snapshot))
    return blockers


def _inspect(client, cfg, target):
    """Read the destination's actual policy state without writing anything."""
    collection = cfg["qdrant"]["collection"]
    mode = cfg["qdrant"]["mode"]
    if not client.collection_exists(collection):
        return {"initialized": False, "collection": collection}
    schema = _schema(client, collection)
    identity = _identity(client, collection)
    blockers = []
    if schema is None:
        blockers.append({"code": "collection_layout", "detail": "a named dense vector is required"})
    else:
        if schema["size"] != target["dimensions"]:
            blockers.append(
                {
                    "code": "dimension_mismatch",
                    "detail": "the collection vector size differs from the configured model",
                }
            )
        if schema["distance"].lower() != str(target["distance"]).lower():
            blockers.append(
                {
                    "code": "distance_mismatch",
                    "detail": "the collection distance differs from the configured distance",
                }
            )
        if schema["datatype"] not in {"float32", "float"}:
            blockers.append(
                {
                    "code": "vector_datatype",
                    "detail": "only float32 dense vectors are supported for in-place upgrade",
                }
            )
    if identity is None:
        blockers.append(
            {"code": "identity_missing", "detail": "no trusted embedding fingerprint is stored"}
        )
    elif identity["embedding_fingerprint"] != target["embedding_fingerprint"]:
        blockers.append(
            {
                "code": "fingerprint_mismatch",
                "detail": "the stored embedding pipeline differs; in-place upgrade is refused",
            }
        )
    return {
        "initialized": True,
        "collection": collection,
        "schema": schema,
        "identity": identity,
        "points": _memory_count(client, collection),
        "indexes": strategy.observe(client, collection, mode),
        "strict_mode": _strict_mode(client, collection),
        "blockers": blockers,
    }


def _plan_body(cfg, namespace, target, observed, source):
    """Return the stable part of a plan; a change here invalidates the plan."""
    return {
        "namespace": namespace,
        "collection": cfg["qdrant"]["collection"],
        "backend": cfg["qdrant"]["mode"],
        "config_digest": strategy.config_digest(cfg),
        "target": target,
        "source_policy": source,
        "checkpoint": {
            "points": observed.get("points"),
            "identity": observed.get("identity"),
            "schema": observed.get("schema"),
        },
        "index_status": observed.get("indexes", {}).get("status"),
    }


def _blockers(observed, snapshot, plan):
    """Combine destination and bookkeeping blockers into one ordered list."""
    items = list(observed.get("blockers", []))
    if not observed.get("initialized"):
        items.append(
            {
                "code": "not_initialized",
                "detail": "run init first; upgrade never creates a collection",
            }
        )
    if plan["source_policy"]["status"] == "corrupt":
        items.append({"code": "policy_corrupt", "detail": "the saved policy record is unreadable"})
    conflicts = [
        entry["field"]
        for entry in observed.get("indexes", {}).get("entries", [])
        if entry["status"] == strategy.CONFLICT
    ]
    if conflicts:
        items.append(
            {
                "code": "index_conflict",
                "detail": "conflicting payload index types on: " + ", ".join(conflicts),
            }
        )
    items.extend(_unresolved_work(snapshot))
    return items


def _probe(cfg, target):
    """Read one destination, reporting an unreachable backend as a blocker."""
    try:
        client = build_client(cfg)
    except Exception as exc:
        return _unreachable(cfg, exc), {"version": None, "source": "unavailable"}
    try:
        return _inspect(client, cfg, target), _server_version(client, cfg["qdrant"]["mode"])
    except Exception as exc:
        return _unreachable(cfg, exc), {"version": None, "source": "unavailable"}
    finally:
        client.close()


def _unreachable(cfg, exc):
    """Describe an unreachable backend without echoing its endpoint or credential."""
    return {
        "initialized": None,
        "collection": cfg["qdrant"]["collection"],
        "unreachable": True,
        "error_type": type(exc).__name__,
        "blockers": [
            {
                "code": "backend_unreachable",
                "detail": "the destination could not be inspected; capabilities are unknown",
            }
        ],
    }


def _destination_matches(state, cfg, namespace):
    """Return whether a saved plan still names this profile and destination."""
    plan = state.get("plan") or {}
    current = {
        "namespace": namespace,
        "collection": cfg["qdrant"]["collection"],
        "backend": cfg["qdrant"]["mode"],
        "config_digest": strategy.config_digest(cfg),
    }
    return all(plan.get(key) == value for key, value in current.items())


def _plan_is_stale(state, cfg, namespace):
    """Report a saved plan that can no longer describe this destination."""
    return not _destination_matches(state, cfg, namespace)


def _in_progress(state):
    """Return whether a saved plan has started and has not reached ready."""
    if state.get("phase") == PHASE_READY:
        return False
    if state.get("phase") in {PHASE_APPLYING, PHASE_VERIFYING, PHASE_FAILED}:
        return True
    return any(
        (record or {}).get("status") != "pending" for record in (state.get("steps") or {}).values()
    )


def _existing_plan_result(state, cfg, namespace, shared):
    """Report an in-progress plan without overwriting its recovery record."""
    stale = _plan_is_stale(state, cfg, namespace)
    plan = state.get("plan") or {}
    saved = [
        item
        for item in state.get("blockers", [])
        if item.get("code") not in {"shared_pending", "shared_owner_receipt"}
    ]
    blockers = saved + list(shared)
    return {
        "ok": not blockers,
        "phase": state.get("phase"),
        "plan_id": state.get("plan_id"),
        "persisted": False,
        "reused_existing_plan": True,
        "collection": plan.get("collection"),
        "backend": plan.get("backend"),
        "source_policy": plan.get("source_policy"),
        "target": plan.get("target"),
        "checkpoint": plan.get("checkpoint"),
        "blockers": blockers,
        "warnings": state.get("warnings", []),
        "steps": state.get("steps", {}),
        "failure": state.get("failure"),
        "backup": (state.get("steps") or {}).get("backup", {}),
        "next": "qdrant-memory upgrade plan --replace"
        if stale
        else f"qdrant-memory upgrade resume --plan {state.get('plan_id')}",
    }


def _archive_state(home, namespace, state):
    """Keep a replaced plan and its recovery reference outside the active slot."""
    plan_id = state.get("plan_id")
    if not plan_id:
        raise UpgradeStateError("The saved plan has no identifier; inspect the control plane.")
    archived = dict(state)
    archived["archived_at"] = now()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    suffix = f"{STATE_SUFFIX}-replaced-{plan_id}-{stamp}-{uuid.uuid4().hex[:8]}"
    strategy.write_state(home, namespace, suffix, archived)
    return {
        "plan_id": plan_id,
        "phase": state.get("phase"),
        "backup": (state.get("steps") or {}).get("backup"),
        "archive": suffix,
    }


def plan_destination(home, cfg, *, persist=True, destination_policy=None, replace=False):
    """Inspect one destination and save the plan that ``apply`` must reference.

    An interrupted plan keeps its phase, failure, steps and recovery reference.
    A normal plan reports that record. ``replace`` archives it first, then saves
    a fresh plan so an operator never has to delete a control-plane file by hand.
    """
    from .config import ledger_namespace

    namespace = ledger_namespace(cfg)
    target = strategy.desired_policy(cfg)
    lease = WriterLease.for_config(home, cfg, destination_policy=destination_policy)
    try:
        existing = strategy.read_state(home, namespace, STATE_SUFFIX)
        if existing and _in_progress(existing) and not replace:
            shared = _shared_work(_ledger_snapshot(home, namespace))
            return _existing_plan_result(existing, cfg, namespace, shared)
        snapshot = _ledger_snapshot(home, namespace)
        source = strategy.effective_summary(home, namespace, cfg)
        if cfg["qdrant"]["mode"] == "embedded" and not Path(cfg["qdrant"]["path"]).is_dir():
            # Opening the local client would create the directory; a plan must not.
            observed = {"initialized": False, "collection": cfg["qdrant"]["collection"]}
            version = {"version": None, "source": "unavailable"}
        else:
            observed, version = _probe(cfg, target)
        plan = _plan_body(cfg, namespace, target, observed, source)
        state = {
            "schema": 1,
            "plan_id": digest(plan),
            "instance_id": uuid.uuid4().hex,
            "created_at": now(),
            "updated_at": now(),
            "phase": PHASE_PLANNED,
            "plan": plan,
            "observed": observed,
            "ledger": snapshot,
            "backend_version": version,
            "blockers": [],
            "steps": {name: {"status": "pending"} for name in STEPS},
            "warnings": _warnings(cfg, source, observed),
        }
        state["blockers"] = _blockers(observed, snapshot, plan)
        if persist:
            # Publish before the next writer can start, so a plan can never
            # overwrite progress another pass recorded after the lease.
            keep_previous = existing is not None and (replace or _in_progress(existing))
            if existing and (keep_previous or existing.get("phase") == PHASE_READY):
                state["previous_plan"] = _archive_state(home, namespace, existing)
            strategy.write_state(home, namespace, STATE_SUFFIX, state)
        return state
    finally:
        lease.close()


def _warnings(cfg, source, observed):
    """Report conditions that do not block an upgrade but must stay visible."""
    warnings = []
    if (
        observed.get("strict_mode") == "enabled"
        and observed.get("indexes", {}).get("status") == strategy.MISSING
    ):
        warnings.append(
            {
                "code": "strict_mode_missing_indexes",
                "detail": "strict mode rejects unfiltered filters until indexes exist",
            }
        )
    if cfg["qdrant"]["mode"] == "embedded":
        warnings.append(
            {
                "code": "embedded_exclusive",
                "detail": "inspection needs exclusive access to the local store",
            }
        )
    if source["status"] == "pending":
        warnings.append(
            {
                "code": "policy_pending",
                "detail": "a saved policy record does not match the configured target",
            }
        )
    return warnings


def _load_state(home, namespace):
    """Return the saved upgrade state or raise the actionable refusal."""
    state = strategy.read_state(home, namespace, STATE_SUFFIX)
    if not state or state.get("schema") != 1:
        raise UpgradeStateError(
            "No saved upgrade plan for this destination; run `upgrade plan` first."
        )
    return state


def _require_same_destination(state, cfg, namespace):
    """Refuse a plan whose profile, destination or settings have moved."""
    if not _destination_matches(state, cfg, namespace):
        raise UpgradePlanStaleError(
            "The saved plan no longer matches this profile or destination; "
            "run `upgrade plan --replace` to keep its recovery record and plan again."
        )


def _require_authorization(state, cfg, args):
    """Require provable write isolation and a recovery point for remote backends."""
    if cfg["qdrant"]["mode"] == "embedded":
        return
    if not getattr(args, "confirm_writers_isolated", False):
        raise UpgradeRefusedError(
            "Remote apply requires --confirm-writers-isolated after stopping every "
            "other Owner, gateway and process that writes this destination."
        )
    if not getattr(args, "accept_operator_backup", False):
        raise UpgradeRefusedError(
            "Remote apply requires --accept-operator-backup: take a provider-side "
            "snapshot at the same stopped-writer point, or stop here."
        )
    if state.get("plan", {}).get("backend") != cfg["qdrant"]["mode"]:
        raise UpgradePlanStaleError(
            "The backend mode changed after the plan was saved; "
            "run `upgrade plan --replace` to keep the old recovery record."
        )


class _Context:
    """Carry the validated inputs of one apply or resume pass."""

    def __init__(self, home, cfg, namespace, state, client, args):
        """Bind the plan, live client and operator flags for the step runner."""
        self.home = home
        self.cfg = cfg
        self.namespace = namespace
        self.state = state
        self.client = client
        self.args = args

    @property
    def collection(self):
        """Return the destination collection name."""
        return self.cfg["qdrant"]["collection"]

    @property
    def backend(self):
        """Return the destination backend mode."""
        return self.cfg["qdrant"]["mode"]

    def save(self):
        """Persist the current phase and step records atomically."""
        self.state["updated_at"] = now()
        strategy.write_state(self.home, self.namespace, STATE_SUFFIX, self.state)

    def mark(self, step, status, **detail):
        """Record one step transition and persist it before the next action."""
        record = {"status": status, "at": now()}
        record.update(detail)
        self.state["steps"][step] = record
        self.save()


def _backup(ctx):
    """Create and verify the recovery point at the stopped-writer boundary."""
    if ctx.backend != "embedded":
        return {"outcome": "operator-required", "detail": REMOTE_BACKUP_NOTE}
    return _backup_embedded(ctx)


def _backup_embedded(ctx):
    """Copy the closed local store, ledger and policy records, then reopen them."""
    checkpoint = ctx.state["plan"]["checkpoint"]
    root = Path(ctx.home).resolve() / "qdrant-memory" / "backups"
    target = root / f"{_stamp()}-{ctx.state['instance_id'][:12]}"
    for stale in root.glob(f"*-{ctx.state['instance_id'][:12]}.partial"):
        if stale.is_dir() and not stale.is_symlink():
            shutil.rmtree(stale)
    partial = target.with_name(target.name + ".partial")
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True, mode=0o700)
    storage = Path(ctx.cfg["qdrant"]["path"])
    # Close every local SQLite handle before the copy, then reopen afterwards.
    ctx.client.close()
    try:
        shutil.copytree(storage, partial / "qdrant")
        ledger_dir = Path(ctx.home).resolve() / "qdrant-memory"
        for name in ("state.db", "state.db-wal", "state.db-shm"):
            source = ledger_dir / name
            if source.is_file():
                shutil.copy2(source, partial / name)
        policy = strategy.directory(ctx.home)
        if policy.is_dir():
            shutil.copytree(policy, partial / "strategy")
        for name in ("qdrant-memory.json", ".setup-owner.json"):
            source = Path(ctx.home).resolve() / name
            if source.is_file():
                shutil.copy2(source, partial / name)
    finally:
        ctx.client = build_client(ctx.cfg)
    snapshot = _backup_snapshot(partial / "qdrant", ctx.collection)
    if snapshot["points"] != checkpoint.get("points"):
        raise UpgradeRefusedError(
            "The recovery point does not reopen with the planned point count; "
            "stop the maintenance window and inspect the local store."
        )
    _write_backup_marker(partial, ctx, snapshot)
    if target.exists():
        shutil.rmtree(target)
    os.replace(partial, target)
    return {
        "outcome": "created",
        "path": str(target.relative_to(Path(ctx.home).resolve())),
        "points": snapshot["points"],
        "verified": True,
    }


def _write_backup_marker(directory, ctx, snapshot):
    """Publish the completion marker last, so a partial copy is never adopted."""
    marker = {
        "schema": 1,
        "plan_id": ctx.state["plan_id"],
        "instance_id": ctx.state.get("instance_id"),
        "collection": ctx.collection,
        "points": snapshot["points"],
        "identity": snapshot["identity"],
        "created_at": now(),
    }
    descriptor = os.open(directory / BACKUP_MARKER, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(marker, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


def _backup_snapshot(path, collection):
    """Open a copied embedded store and read the count and identity sentinel."""
    client = build_client({"qdrant": {"mode": "embedded", "path": str(path)}})
    try:
        if not client.collection_exists(collection):
            raise UpgradeRefusedError(
                "The recovery point does not contain the collection under upgrade."
            )
        return {
            "points": _memory_count(client, collection),
            "identity": _identity(client, collection),
        }
    finally:
        client.close()


def _indexes(ctx):
    """Create only the managed indexes that are missing, then re-read the schema."""
    observation = strategy.observe(ctx.client, ctx.collection, ctx.backend)
    conflicts = [entry for entry in observation["entries"] if entry["status"] == strategy.CONFLICT]
    if conflicts:
        fields = ", ".join(entry["field"] for entry in conflicts)
        raise UpgradeRefusedError(
            "Existing payload indexes conflict with the managed policy on: "
            f"{fields}. This Runtime never deletes or rebuilds them automatically; "
            "review the field types and remove the conflicting index by hand."
        )
    created = []
    for entry in observation["entries"]:
        if entry["status"] != strategy.MISSING:
            continue
        ctx.client.create_payload_index(
            ctx.collection, field_name=entry["field"], field_schema=entry["expected"], wait=True
        )
        created.append(entry["field"])
    if observation["status"] == strategy.NOT_APPLICABLE:
        return {"created": [], "verified": observation}
    return {"created": created, "verified": _require_indexes(ctx)}


def _require_indexes(ctx):
    """Re-read the payload schema and return the verified observation."""
    observation = strategy.observe(ctx.client, ctx.collection, ctx.backend)
    drift = strategy.managed_drift(observation)
    if drift:
        fields = ", ".join(entry["field"] for entry in drift)
        raise UpgradeRefusedError(f"Payload indexes are still incomplete after creation: {fields}")
    return observation


def _sample_point(ctx):
    """Return one stored memory point used to prove scope filters still work."""
    rows, _ = ctx.client.scroll(ctx.collection, limit=8, with_payload=True, with_vectors=False)
    for row in rows:
        if str(row.id) == IDENTITY_ID:
            continue
        payload = row.payload or {}
        if payload.get("user_id"):
            return payload
    return None


def _filter_probe(ctx, sample):
    """Run the mandatory scope filter against real data and require a match."""
    from qdrant_client import models as m

    conditions: list = [
        m.FieldCondition(key="user_id", match=m.MatchValue(value=sample["user_id"]))
    ]
    if sample.get("agent_id") is None:
        conditions.append(m.IsNullCondition(is_null=m.PayloadField(key="agent_id")))
    else:
        conditions.append(
            m.FieldCondition(key="agent_id", match=m.MatchValue(value=sample["agent_id"]))
        )
    count = ctx.client.count(
        ctx.collection, count_filter=m.Filter(must=conditions), exact=True
    ).count
    if count < 1:
        raise UpgradeRefusedError(
            "A scoped filter returned no rows for stored data. Do not drop scope "
            "conditions; resolve the index or backend state first."
        )
    return count


def _verify(ctx):
    """Prove the target policy, identity, data checkpoint and filters hold."""
    observation = _require_indexes(ctx)
    checkpoint = ctx.state["plan"]["checkpoint"]
    identity = _identity(ctx.client, ctx.collection)
    points = _memory_count(ctx.client, ctx.collection)
    if points != checkpoint.get("points"):
        raise UpgradeRefusedError(
            "The point count changed after the plan was saved. Stop every writer "
            "and plan again; this Runtime never activates on a moved checkpoint."
        )
    if (identity or {}).get("embedding_fingerprint") != (checkpoint.get("identity") or {}).get(
        "embedding_fingerprint"
    ):
        raise UpgradeRefusedError("The embedding identity changed during the upgrade.")
    sample = _sample_point(ctx)
    probe = _filter_probe(ctx, sample) if sample else 0
    return {
        "indexes": observation["status"],
        "points": points,
        "scope_probe": probe,
        "sampled": bool(sample),
    }


def _activate(ctx):
    """Commit the effective policy at a new generation after verification."""
    # A resume can reach activation after verify was recorded done. Re-prove the
    # indexes, identity, checkpoint and filters before committing the policy.
    _verify(ctx)
    previous = strategy.read(ctx.home, ctx.namespace) or {}
    generation = int(previous.get("generation") or 0) + 1
    record = {
        "schema": strategy.POLICY_SCHEMA_VERSION,
        "generation": generation,
        "policy": ctx.state["plan"]["target"],
        "activated_at": now(),
        "activated_from": ctx.state["plan_id"],
        "previous": {
            "policy": previous.get("policy"),
            "generation": previous.get("generation"),
        },
    }
    strategy.write(ctx.home, ctx.namespace, record)
    return {"generation": generation, "requires_restart": True}


ACTIONS = {
    "backup": _backup,
    "indexes": _indexes,
    "verify": _verify,
    "activate": _activate,
}


def _backup_done(ctx):
    """Detect a completed backup after a crash before its record was written."""
    record = ctx.state["steps"].get("backup", {})
    path = record.get("path")
    if path and (Path(ctx.home).resolve() / path).is_dir():
        return {}
    recovered = _recover_backup(ctx)
    if recovered:
        return {"path": recovered, "verified": True, "recovered": True}
    return None


def _recover_backup(ctx):
    """Adopt a verified backup that was copied before its step record landed."""
    root = Path(ctx.home).resolve() / "qdrant-memory" / "backups"
    if not root.is_dir():
        return None
    checkpoint = ctx.state["plan"]["checkpoint"]
    prefixes = [ctx.state["instance_id"][:12]]
    legacy = bool(ctx.state.get("legacy_backup"))
    if legacy:
        prefixes.append(ctx.state["plan_id"][:12])
    expected_identity = (checkpoint.get("identity") or {}).get("embedding_fingerprint")
    seen = set()
    for prefix in prefixes:
        for candidate in sorted(root.glob(f"*-{prefix}"), reverse=True):
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            if candidate.name.endswith(".partial"):
                continue
            try:
                marker_path = candidate / BACKUP_MARKER
                if marker_path.is_file():
                    marker = json.loads(marker_path.read_text(encoding="utf-8"))
                    if marker.get("instance_id") != ctx.state.get("instance_id"):
                        continue
                elif not (legacy and prefix == ctx.state["plan_id"][:12]):
                    continue
                else:
                    # A 0.7.0 backup has no marker. Require the ledger copy when
                    # the saved plan says a ledger existed.
                    if (
                        ctx.state.get("ledger", {}).get("present")
                        and not (candidate / "state.db").is_file()
                    ):
                        continue
                    marker = {}
                snapshot = _backup_snapshot(candidate / "qdrant", ctx.collection)
            except Exception:
                continue
            if marker.get("plan_id") not in (None, ctx.state["plan_id"]):
                continue
            if marker.get("collection") not in (None, ctx.collection):
                continue
            if marker.get("points") not in (None, snapshot["points"]):
                continue
            if (marker.get("identity") or {}).get("embedding_fingerprint") not in (
                None,
                expected_identity,
            ):
                continue
            if snapshot["points"] != checkpoint.get("points"):
                continue
            if (snapshot.get("identity") or {}).get("embedding_fingerprint") != expected_identity:
                continue
            return str(candidate.relative_to(Path(ctx.home).resolve()))
    return None


def _indexes_done(ctx):
    """Detect created indexes that were not recorded before a crash."""
    if ctx.backend == "embedded":
        # Embedded storage has no payload indexes; the step is recorded as not applicable.
        return {}
    observation = strategy.observe(ctx.client, ctx.collection, ctx.backend)
    return {} if not strategy.managed_drift(observation) else None


def _activate_done(ctx):
    """Detect and verify a policy record committed by an interrupted activation.

    The record must carry the expected target and plan identifier, and the live
    destination must still satisfy the post-conditions. A single marker is not
    enough to report success.
    """
    record = strategy.read(ctx.home, ctx.namespace) or {}
    if record.get("activated_from") != ctx.state["plan_id"]:
        return None
    if record.get("policy") != ctx.state["plan"]["target"]:
        return None
    generation = record.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        return None
    _verify(ctx)
    return {"generation": generation, "verified": True}


DETECTORS = {
    "backup": _backup_done,
    "indexes": _indexes_done,
    "verify": None,
    "activate": _activate_done,
}


def _run_steps(ctx):
    """Run every step once, skipping recorded and detectable completions."""
    for name in STEPS:
        record = ctx.state["steps"].get(name, {"status": "pending"})
        if record.get("status") == "done":
            if name == "activate" and _activate_done(ctx) is None:
                raise UpgradeStateError(
                    "The committed policy record no longer matches the saved plan; "
                    "run `upgrade plan --replace`."
                )
            continue
        detector = DETECTORS[name]
        detail = detector(ctx) if detector is not None else None
        if detail is not None:
            ctx.mark(name, "done", detected=True, **detail)
            continue
        ctx.mark(name, "running")
        detail = ACTIONS[name](ctx)
        ctx.mark(name, "done", **detail)
    ctx.state["phase"] = PHASE_READY
    ctx.state["ready_at"] = now()
    ctx.save()


def _guarded_apply(args, home, cfg, namespace):
    """Validate a saved plan and run it under one writer lease."""
    state = _load_state(home, namespace)
    _validate_apply_state(state, args, cfg, namespace)
    planned_revision = digest(state)
    lease = WriterLease.for_config(home, cfg, destination_policy=_destination_policy(args))
    try:
        # Another plan may have replaced this record before the lease was
        # acquired. Re-read under the lock and never write the older object.
        state = _load_state(home, namespace)
        _validate_apply_state(state, args, cfg, namespace)
        if digest(state) != planned_revision:
            raise UpgradePlanStaleError(
                "The saved plan was replaced before the writer lease was acquired; "
                "run the same apply command again."
            )
        if state["phase"] == PHASE_READY:
            pending = _shared_work(_ledger_snapshot(home, namespace))
            if pending:
                raise UpgradeBlockedError(pending)
            return {"ok": True, "phase": PHASE_READY, "already_applied": True}
        if "instance_id" not in state:
            state["instance_id"] = uuid.uuid4().hex
            state["legacy_backup"] = True
        snapshot = _ledger_snapshot(home, namespace)
        client = build_client(cfg)
        context = None
        try:
            observed = _inspect(client, cfg, state["plan"]["target"])
            current_blockers = _blockers(observed, snapshot, state["plan"])
            if current_blockers:
                raise UpgradeBlockedError(current_blockers)
            _require_live_matches(state, observed)
            context = _Context(home, cfg, namespace, state, client, args)
            context.state["phase"] = PHASE_APPLYING
            context.save()
            try:
                _run_steps(context)
            except Exception as exc:
                _record_failure(home, namespace, exc, state["plan_id"])
                raise
            return _result(context)
        finally:
            # An embedded backup closes and reopens the store, so close the
            # client that is current at the end of the pass.
            current = context.client if context is not None else client
            current.close()
            if current is not client:
                try:
                    client.close()
                except Exception:
                    pass
    finally:
        lease.close()


def _validate_apply_state(state, args, cfg, namespace):
    """Require one live plan identifier, phase and matching destination."""
    if args.plan != state.get("plan_id"):
        raise UpgradePlanStaleError(
            "The requested plan does not match the saved plan for this destination."
        )
    if state["phase"] == PHASE_READY:
        return
    if state["phase"] not in LIVE_PHASES:
        raise UpgradeStateError("The saved plan has an unsupported phase; run `upgrade plan`.")
    _require_same_destination(state, cfg, namespace)
    _require_authorization(state, cfg, args)


def _require_live_matches(state, observed):
    """Refuse to start when the destination moved since the plan was saved."""
    checkpoint = state["plan"]["checkpoint"]
    if observed.get("blockers"):
        raise UpgradeBlockedError(observed["blockers"])
    if observed.get("points") != checkpoint.get("points"):
        raise UpgradePlanStaleError(
            "The destination changed since the plan was saved; run "
            "`upgrade plan --replace` to keep the old recovery record and plan again."
        )
    if (observed.get("identity") or {}) != (checkpoint.get("identity") or {}):
        raise UpgradePlanStaleError(
            "The embedding identity changed since the plan was saved; run "
            "`upgrade plan --replace` to keep the old recovery record and plan again."
        )


def _result(ctx):
    """Return the stable machine-readable outcome of one apply pass."""
    return {
        "ok": True,
        "phase": ctx.state["phase"],
        "plan_id": ctx.state["plan_id"],
        "collection": ctx.collection,
        "backend": ctx.backend,
        "steps": ctx.state["steps"],
        "generation": ctx.state["steps"]["activate"].get("generation"),
        "requires_restart": True,
        "rollback": _rollback_boundary(ctx),
    }


def _rollback_boundary(ctx):
    """State exactly what an application rollback can and cannot undo."""
    return {
        "application_version": "reinstall the previous Runtime release",
        "storage": "no downgrade; restore the recovery point taken at this stop point",
        "point_in_time": "restoring a backup discards writes that landed after it",
        "path": ctx.state["steps"]["backup"].get("path"),
    }


def _destination_policy(args):
    """Load the operator's protected destination binding for this pass."""
    from . import cli

    return cli._destination_policy(args)


def apply_plan(args, home, cfg):
    """Apply or resume a saved plan; both entry points share this implementation."""
    from .config import ledger_namespace

    namespace = ledger_namespace(cfg)
    return _guarded_apply(args, home, cfg, namespace)


def _record_failure(home, namespace, exc, plan_id):
    """Persist a sanitized failure only against the plan that was running."""
    try:
        state = strategy.read_state(home, namespace, STATE_SUFFIX)
        if not state or state.get("plan_id") != plan_id or state.get("phase") == PHASE_READY:
            return
        state["phase"] = PHASE_FAILED
        state["failed_at"] = now()
        state["failure"] = {"type": type(exc).__name__, "retryable": _retryable(exc)}
        strategy.write_state(home, namespace, STATE_SUFFIX, state)
    except Exception:
        # Never mask the upgrade failure that reached this recorder.
        return None
    return state


def _retryable(exc):
    """Report whether the failure is expected to clear without operator action."""
    from .retry import retryable

    return retryable(exc)


def _saved_state(home, cfg):
    """Locate the saved plan for this destination, even without a resolved endpoint."""
    from .config import ledger_namespace

    try:
        namespace = ledger_namespace(cfg)
    except Exception:
        namespace = None
    if namespace:
        state = strategy.read_state(home, namespace, STATE_SUFFIX)
        if state:
            return state, namespace
    matches = [
        record
        for record in strategy.saved_upgrades(home)
        if record.get("plan", {}).get("collection") == cfg["qdrant"]["collection"]
        and record.get("plan", {}).get("backend") == cfg["qdrant"]["mode"]
    ]
    if len(matches) == 1:
        return matches[0], matches[0].get("plan", {}).get("namespace")
    return None, namespace


def status(home, cfg):
    """Report the saved plan and checkpoint without opening the destination."""
    state, namespace = _saved_state(home, cfg)
    effective = (
        strategy.effective_summary(home, namespace, cfg)
        if namespace
        else {"status": "unknown", "target": strategy.desired_policy(cfg)}
    )
    if not state:
        return {
            "phase": "unplanned",
            "effective": effective["status"],
            "next": "qdrant-memory upgrade plan",
        }
    try:
        stale = bool(namespace) and _plan_is_stale(state, cfg, namespace)
    except Exception:
        # Status must stay readable without resolved secrets or a reachable endpoint.
        stale = None
    phase = state.get("phase")
    if phase == PHASE_READY:
        next_command = None
    elif stale:
        next_command = "qdrant-memory upgrade plan --replace"
    elif stale is False and phase in {PHASE_PLANNED, PHASE_FAILED}:
        next_command = "qdrant-memory upgrade apply --plan <plan_id>"
    elif stale is False and phase in {PHASE_APPLYING, PHASE_VERIFYING}:
        next_command = "qdrant-memory upgrade resume --plan <plan_id>"
    elif stale is False:
        next_command = "qdrant-memory upgrade plan --replace"
    else:
        next_command = "qdrant-memory upgrade status"
    return {
        "phase": phase,
        "plan_id": state.get("plan_id"),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "collection": state.get("plan", {}).get("collection"),
        "backend": state.get("plan", {}).get("backend"),
        "blockers": state.get("blockers", []),
        "warnings": state.get("warnings", []),
        "steps": state.get("steps", {}),
        "failure": state.get("failure"),
        "backup_path": (state.get("steps") or {}).get("backup", {}).get("path"),
        "plan_stale": stale,
        "effective": effective["status"],
        "target": effective["target"],
        "requires_restart": phase == PHASE_READY,
        "next": next_command,
    }


def describe(home, cfg, args):
    """Dispatch ``upgrade plan|apply|resume|status`` for both operator entries."""
    action = args.upgrade_action
    if action == "status":
        return status(home, cfg)
    if action == "plan":
        state = plan_destination(
            home,
            cfg,
            persist=not getattr(args, "dry_run", False),
            destination_policy=_destination_policy(args),
            replace=getattr(args, "replace", False),
        )
        if state.get("reused_existing_plan"):
            return state
        return {
            "ok": not state["blockers"],
            "phase": state["phase"],
            "plan_id": state["plan_id"],
            "persisted": not getattr(args, "dry_run", False),
            "reused_existing_plan": False,
            "collection": state["plan"]["collection"],
            "backend": state["plan"]["backend"],
            "source_policy": state["plan"]["source_policy"],
            "target": state["plan"]["target"],
            "indexes": state["observed"].get("indexes"),
            "strict_mode": state["observed"].get("strict_mode"),
            "backend_version": state["backend_version"],
            "checkpoint": state["plan"]["checkpoint"],
            "blockers": state["blockers"],
            "warnings": state["warnings"],
            "previous_plan": state.get("previous_plan"),
            "next": "qdrant-memory upgrade apply --plan " + state["plan_id"],
        }
    return apply_plan(args, home, cfg)
