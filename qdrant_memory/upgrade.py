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

import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from . import strategy
from .models import digest, now
from .ownership import WriterLease
from .qdrant_store import IDENTITY_ID, build_client

PHASE_PLANNED = "planned"
PHASE_APPLYING = "applying"
PHASE_VERIFYING = "verifying"
PHASE_READY = "ready"
PHASE_FAILED = "failed"
LIVE_PHASES = {PHASE_PLANNED, PHASE_APPLYING, PHASE_VERIFYING, PHASE_FAILED}
STEPS = ("backup", "indexes", "verify", "activate")
STATE_SUFFIX = "upgrade"

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
    """Report blockers that make this destination ineligible for 0.7.0."""

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
        "shared_pending": 0,
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
        if "shared_requests_v1" in tables:
            snapshot["shared_pending"] = db.execute(
                "SELECT COUNT(*) FROM shared_requests_v1 WHERE collection=? AND done=0",
                (namespace,),
            ).fetchone()[0]
        return snapshot


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
    if snapshot.get("shared_pending", 0):
        blockers.append(
            {"code": "shared_pending", "detail": "shared requests are still unacknowledged"}
        )
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


def plan_destination(home, cfg, *, persist=True):
    """Inspect one destination and save the plan that ``apply`` must reference."""
    from .config import ledger_namespace

    namespace = ledger_namespace(cfg)
    target = strategy.desired_policy(cfg)
    snapshot = _ledger_snapshot(home, namespace)
    source = strategy.effective_summary(home, namespace, cfg)
    lease = WriterLease.for_config(home, cfg)
    try:
        if cfg["qdrant"]["mode"] == "embedded" and not Path(cfg["qdrant"]["path"]).is_dir():
            # Opening the local client would create the directory; a plan must not.
            observed = {"initialized": False, "collection": cfg["qdrant"]["collection"]}
            version = {"version": None, "source": "unavailable"}
        else:
            observed, version = _probe(cfg, target)
    finally:
        lease.close()
    plan = _plan_body(cfg, namespace, target, observed, source)
    plan_id = digest(plan)
    state = {
        "schema": 1,
        "plan_id": plan_id,
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
        strategy.write_state(home, namespace, STATE_SUFFIX, state)
    return state


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
    plan = state.get("plan") or {}
    current = {
        "namespace": namespace,
        "collection": cfg["qdrant"]["collection"],
        "backend": cfg["qdrant"]["mode"],
        "config_digest": strategy.config_digest(cfg),
    }
    for key, value in current.items():
        if plan.get(key) != value:
            raise UpgradePlanStaleError(
                "The saved plan no longer matches this profile or destination; "
                "run `upgrade plan` again."
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
        raise UpgradePlanStaleError("The backend mode changed after the plan was saved.")


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
    target = root / f"{_stamp()}-{ctx.state['plan_id'][:12]}"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, mode=0o700)
    storage = Path(ctx.cfg["qdrant"]["path"])
    # Close every local SQLite handle before the copy, then reopen afterwards.
    ctx.client.close()
    try:
        shutil.copytree(storage, target / "qdrant")
        ledger_dir = Path(ctx.home).resolve() / "qdrant-memory"
        for name in ("state.db", "state.db-wal", "state.db-shm"):
            source = ledger_dir / name
            if source.is_file():
                shutil.copy2(source, target / name)
        policy = strategy.directory(ctx.home)
        if policy.is_dir():
            shutil.copytree(policy, target / "strategy")
        for name in ("qdrant-memory.json", ".setup-owner.json"):
            source = Path(ctx.home).resolve() / name
            if source.is_file():
                shutil.copy2(source, target / name)
    finally:
        ctx.client = build_client(ctx.cfg)
    reopened = _reopen_backup(target / "qdrant")
    if reopened != checkpoint.get("points"):
        raise UpgradeRefusedError(
            "The recovery point does not reopen with the planned point count; "
            "stop the maintenance window and inspect the local store."
        )
    return {
        "outcome": "created",
        "path": str(target.relative_to(Path(ctx.home).resolve())),
        "points": reopened,
        "verified": True,
    }


def _reopen_backup(path):
    """Open a copied embedded store and return its point count."""
    client = build_client({"qdrant": {"mode": "embedded", "path": str(path)}})
    try:
        names = [item.name for item in client.get_collections().collections]
        if not names:
            return 0
        return _memory_count(client, names[0])
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
    if not path:
        return False
    return (Path(ctx.home).resolve() / path).is_dir()


def _indexes_done(ctx):
    """Detect created indexes that were not recorded before a crash."""
    if ctx.backend == "embedded":
        return False
    return not strategy.managed_drift(strategy.observe(ctx.client, ctx.collection, ctx.backend))


def _activate_done(ctx):
    """Detect a policy record committed by an interrupted activation."""
    record = strategy.read(ctx.home, ctx.namespace) or {}
    return record.get("activated_from") == ctx.state["plan_id"]


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
            continue
        detector = DETECTORS[name]
        if detector is not None and detector(ctx):
            ctx.mark(name, "done", detected=True)
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
    if args.plan != state.get("plan_id"):
        raise UpgradePlanStaleError(
            "The requested plan does not match the saved plan for this destination."
        )
    if state["phase"] == PHASE_READY:
        return {"ok": True, "phase": PHASE_READY, "already_applied": True}
    if state["phase"] not in LIVE_PHASES:
        raise UpgradeStateError("The saved plan has an unsupported phase; run `upgrade plan`.")
    _require_same_destination(state, cfg, namespace)
    _require_authorization(state, cfg, args)
    if state.get("blockers"):
        raise UpgradeBlockedError(state["blockers"])
    lease = WriterLease.for_config(home, cfg, destination_policy=_destination_policy(args))
    try:
        client = build_client(cfg)
        context = None
        try:
            observed = _inspect(client, cfg, state["plan"]["target"])
            _require_live_matches(state, observed)
            context = _Context(home, cfg, namespace, state, client, args)
            context.state["phase"] = PHASE_APPLYING
            context.save()
            _run_steps(context)
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


def _require_live_matches(state, observed):
    """Refuse to start when the destination moved since the plan was saved."""
    checkpoint = state["plan"]["checkpoint"]
    if observed.get("blockers"):
        raise UpgradeBlockedError(observed["blockers"])
    if observed.get("points") != checkpoint.get("points"):
        raise UpgradePlanStaleError(
            "The destination changed since the plan was saved. Stop every writer "
            "and run `upgrade plan` again."
        )
    if (observed.get("identity") or {}) != (checkpoint.get("identity") or {}):
        raise UpgradePlanStaleError("The embedding identity changed since the plan was saved.")


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
    try:
        return _guarded_apply(args, home, cfg, namespace)
    except Exception as exc:
        _record_failure(home, namespace, exc)
        raise


def _record_failure(home, namespace, exc):
    """Persist a sanitized failure so ``status`` shows where the pass stopped."""
    state = strategy.read_state(home, namespace, STATE_SUFFIX)
    if not state or state.get("phase") == PHASE_READY:
        return
    state["phase"] = PHASE_FAILED
    state["failed_at"] = now()
    state["failure"] = {"type": type(exc).__name__, "retryable": _retryable(exc)}
    try:
        strategy.write_state(home, namespace, STATE_SUFFIX, state)
    except OSError:
        return None
    return state


def _retryable(exc):
    """Report whether the failure is expected to clear without operator action."""
    from .retry import retryable

    return retryable(exc)


def status(home, cfg):
    """Report the saved plan and checkpoint without opening the destination."""
    from .config import ledger_namespace

    namespace = ledger_namespace(cfg)
    state = strategy.read_state(home, namespace, STATE_SUFFIX)
    effective = strategy.effective_summary(home, namespace, cfg)
    if not state:
        return {
            "phase": "unplanned",
            "effective": effective["status"],
            "next": "qdrant-memory upgrade plan",
        }
    return {
        "phase": state.get("phase"),
        "plan_id": state.get("plan_id"),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "collection": state.get("plan", {}).get("collection"),
        "backend": state.get("plan", {}).get("backend"),
        "blockers": state.get("blockers", []),
        "warnings": state.get("warnings", []),
        "steps": state.get("steps", {}),
        "failure": state.get("failure"),
        "effective": effective["status"],
        "target": effective["target"],
        "requires_restart": state.get("phase") == PHASE_READY,
        "next": "qdrant-memory upgrade apply --plan <plan_id>"
        if state.get("phase") in {PHASE_PLANNED, PHASE_FAILED}
        else "qdrant-memory upgrade resume --plan <plan_id>"
        if state.get("phase") in {PHASE_APPLYING, PHASE_VERIFYING}
        else None,
    }


def describe(home, cfg, args):
    """Dispatch ``upgrade plan|apply|resume|status`` for both operator entries."""
    action = args.upgrade_action
    if action == "status":
        return status(home, cfg)
    if action == "plan":
        state = plan_destination(home, cfg, persist=not getattr(args, "dry_run", False))
        return {
            "ok": not state["blockers"],
            "phase": state["phase"],
            "plan_id": state["plan_id"],
            "persisted": not getattr(args, "dry_run", False),
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
            "next": "qdrant-memory upgrade apply --plan " + state["plan_id"],
        }
    return apply_plan(args, home, cfg)
