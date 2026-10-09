"""Hermes CLI commands. No background workers or LLM calls in maintenance mode."""

import hashlib
import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path

from .config import (
    active_home,
    backend_destination,
    embedding_config,
    ledger_namespace,
    load_config,
)
from .embedding import build_embedder
from .inventory import (
    ExportPublicationUnsupportedError,
    ExportTargetExistsError,
    memory_view,
    write_export,
)
from .ledger import Ledger
from .migration import (
    MigrationIncompleteError,
    MigrationSupersededError,
    migrate,
    plan,
    read_json_records,
    read_qdrant_records,
    source_client,
    source_settings,
    verify_collection,
    verify_manifest,
)
from .models import Scope
from .ownership import WriterBusyError, WriterLease
from .progress import MigrationProgress, report
from .qdrant_store import (
    IDENTITY_ID,
    INDEXES,
    CollectionCompatibilityError,
    CollectionNotInitializedError,
    QdrantStore,
    build_client,
)
from .reset import (
    INTENT_SCHEMA_VERSION,
    ResetRecoveryRequiredError,
    ScopeDeleteIncompleteError,
    ScopeDeleteLegacyIntentError,
    ScopeDeleteRecoveryRequiredError,
    ScopeDeleteRefusedError,
    begin_destination_reset,
    begin_scope_delete,
    finish_scope_delete,
    has_pending_reset_file,
    parse_scope_delete_intent,
    pending_scope_deletes,
    pending_scope_deletes_file,
    resolve_legacy_scope_delete,
    resume_destination_reset,
    scope_delete_intent,
    scope_delete_key,
)
from .retry import safe_error
from .runtime import Runtime


class InitializationChoiceRequiredError(ValueError):
    """Require an explicit existing-collection choice outside a terminal."""


class DeleteConfirmationRequiredError(ValueError):
    """Require explicit confirmation before deleting a whole memory scope."""


class ScopeSelectionError(ValueError):
    """Reject scope arguments that cannot identify a user-anchored scope."""


def existing_collection_action(args, collection):
    """Confirm reuse by default; clearing requires an explicit user choice."""
    action = getattr(args, "existing", None)
    if action in {"use", "clear"}:
        return action
    if not sys.stdin.isatty():
        raise InitializationChoiceRequiredError()
    print(
        f"Collection {collection!r} already exists. Use existing or clear ALL its data and rebuild? "
        "[use/clear] (default: use): ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    answer = sys.stdin.readline()
    if not answer:
        raise InitializationChoiceRequiredError()
    action = answer.strip().lower() or "use"
    if action not in {"use", "clear"}:
        raise InitializationChoiceRequiredError()
    return action


def register_cli(subparser):
    """Register maintenance and migration subcommands without starting a provider."""
    commands = subparser.add_subparsers(dest="qdrant_command", required=True)
    for name in ("status", "init", "stats", "doctor", "verify", "retry"):
        parser = commands.add_parser(name)
        parser.add_argument("--collection")
        if name == "init":
            parser.add_argument(
                "--existing",
                choices=["use", "clear"],
                help="Existing collection: use after validation, or delete all data and rebuild",
            )
        parser.set_defaults(func=main)
    parser = commands.add_parser("migrate")
    parser.add_argument("source", choices=["mem0"])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source-json", type=Path)
    group.add_argument("--source-qdrant-collection")
    parser.add_argument("--source-config", type=Path)
    parser.add_argument("--target-collection")
    parser.add_argument(
        "--reembed", action="store_true", help="Re-embed source texts (the default)"
    )
    parser.add_argument(
        "--reuse-vectors",
        action="store_true",
        help="Reserved: not implemented in 0.1; re-embedding is required",
    )
    for flag in ("resume", "retry-failed", "dry-run", "verify"):
        parser.add_argument("--" + flag, action="store_true")
    parser.add_argument("--oversize", choices=["reject", "truncate"], default="reject")
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress migration progress on stderr"
    )
    parser.set_defaults(func=main)
    parser = commands.add_parser("list", help="List stored memories without changing them")
    parser.add_argument("--collection")
    scope_arguments(parser)
    parser.add_argument("--limit", type=int, default=200, help="Maximum memory records to return")
    parser.set_defaults(func=main)
    parser = commands.add_parser(
        "export", help="Write stored memories as portable Mem0-importable JSON"
    )
    parser.add_argument("--collection")
    scope_arguments(parser)
    parser.add_argument("--output", type=Path, required=True, help="Export file path")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing export file")
    parser.set_defaults(func=main)
    parser = commands.add_parser("delete-all", help="Durably delete every memory in one scope")
    parser.add_argument("--collection")
    scope_arguments(parser)
    parser.add_argument(
        "--all-agents",
        action="store_true",
        help="Delete every agent scope of the user, including the no-agent scope",
    )
    parser.add_argument(
        "--resolve-legacy",
        choices=["single_agent", "all_agents"],
        help="Disambiguate a pre-versioned deletion intent before resuming it",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Authorize deleting every memory in the selected scope",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report the scope count without deleting"
    )
    parser.set_defaults(func=main)


def scope_arguments(parser):
    """Add optional user/agent scope selection shared by inventory commands."""
    parser.add_argument("--user", help="Memory scope user ID")
    parser.add_argument(
        "--agent",
        help="Memory scope agent ID; any string, including a literal '*', is a "
        "valid agent id. Omit to select the profile-wide (no agent) scope",
    )


def summary(manifest):
    """Return manifest metadata without per-record identifiers and hashes."""
    return {key: value for key, value in manifest.items() if key != "records"}


def local_status(cfg, home):
    """Describe local configuration without contacting external services."""
    return {
        "provider": "qdrant-memory",
        "mode": cfg["qdrant"]["mode"],
        "collection": cfg["qdrant"]["collection"],
        "llm_mode": cfg["llm"]["mode"],
        "embedding": {k: embedding_config(cfg)[k] for k in ("provider", "model", "dimensions")},
        "ledger_exists": (home / "qdrant-memory" / "state.db").exists(),
    }


def migration_source(args, cfg, home):
    """Read a migration source and close its client before opening the target."""
    if args.reuse_vectors:
        raise ValueError(
            "Vector reuse is refused: Mem0 sources lack a trusted pipeline fingerprint; re-embed"
        )
    if args.source_json:
        return (
            read_json_records(args.source_json),
            "mem0-json",
            str(args.source_json.resolve()),
            hashlib.sha256(args.source_json.read_bytes()).hexdigest(),
        )
    source_q = source_settings(cfg, home, args.source_config)
    source_q["collection"] = args.source_qdrant_collection
    if source_q["collection"] == cfg["qdrant"]["collection"] and backend_destination(
        source_q
    ) == backend_destination(cfg["qdrant"]):
        raise ValueError("Source and target collections must be isolated")
    client = source_client(source_q)
    try:
        records = list(read_qdrant_records(client, args.source_qdrant_collection))
        return records, "mem0-qdrant", ledger_namespace({"qdrant": source_q}), None
    finally:
        client.close()


def verify_target(store, ledger):
    """Verify target payloads and the latest destination-specific migration manifest."""
    result = verify_collection(store)
    manifests = [m for m in ledger.manifests() if m["target_collection"] == store.collection]
    if manifests:
        result["migration"] = verify_manifest(store, manifests[-1])
        result["ok"] = result["ok"] and result["migration"]["ok"]
    return result


def diagnostic_status(store, ledger, cfg, command):
    """Report counts, ledger metrics and optional collection/index readiness."""
    result = {
        "points_count": store.count(),
        "vector_dim": store.embedder.dimensions,
        "ledger": ledger.stats(),
        "metrics": ledger.metrics(),
        "embedding_fingerprint": store.embedder.fingerprint,
    }
    if command in {"doctor", "init"}:
        info = store.client.get_collection(store.collection)
        index_ok = cfg["qdrant"]["mode"] == "embedded" or set(INDEXES) <= set(info.payload_schema)
        result.update(
            hermes_contract="MemoryProvider + scoped threads + ctx.llm",
            embedding_probe="OK",
            payload_indexes="not applicable to embedded"
            if cfg["qdrant"]["mode"] == "embedded"
            else index_ok,
            llm_mode=cfg["llm"]["mode"],
            ok=index_ok,
        )
    return result


def execute_command(args, runtime, source, progress=None):
    """Dispatch a maintenance command using an initialized store and ledger."""
    command = args.qdrant_command
    if command == "migrate":
        records, source_type, source_identifier, checksum = source
        return summary(
            migrate(
                runtime,
                records,
                source_type,
                source_identifier,
                checksum,
                args.resume,
                args.retry_failed,
                args.verify,
                progress=progress,
            )
        )
    if command == "list":
        return inventory_list(runtime, args)
    if command == "export":
        return inventory_export(runtime, args)
    if command == "delete-all":
        return scoped_delete_all(runtime, args)
    if command == "verify":
        return verify_target(runtime.store, runtime.ledger)
    if command == "retry":
        # Raw events require trusted host LLM calls on the next provider startup.
        runtime.ledger.retry_failed()
        runtime.commit(r["idempotency_key"] for r in runtime.ledger.iter_rows("operations"))
    return diagnostic_status(runtime.store, runtime.ledger, runtime.cfg, command)


def scope_from(args):
    """Build an explicit memory scope from command arguments, if one is selected."""
    user = getattr(args, "user", None)
    agent = getattr(args, "agent", None)
    if agent is not None and not user:
        raise ScopeSelectionError("--agent requires --user; scopes are user-anchored")
    if user is None:
        return None
    return Scope(user, agent)


def inventory_list(runtime, args):
    """Return a bounded read-only inventory of stored memories and ledger backlog."""
    scope = scope_from(args)
    limit = int(args.limit)
    if limit < 1:
        raise ValueError("--limit must be at least 1")
    total = runtime.store.count(scope)
    memories = []
    for point in runtime.store.scroll(scope):
        memories.append(memory_view(point))
        if len(memories) >= limit:
            break
    operations = runtime.ledger.stats()["operations"]
    return {
        "collection": runtime.store.collection,
        "scope": scope.as_dict() if scope else None,
        "count": {
            "total": total,
            "returned": len(memories),
            "truncated": total > len(memories),
        },
        "ledger": {
            "pending_operations": operations.get("PENDING", 0),
            "failed_operations": operations.get("FAILED", 0),
        },
        "memories": memories,
    }


def inventory_export(runtime, args):
    """Stream every stored memory to a portable export accepted by migrate mem0."""
    scope = scope_from(args)
    count = write_export(
        args.output, runtime.store.collection, runtime.store.scroll(scope), force=args.force
    )
    return {
        "collection": runtime.store.collection,
        "scope": scope.as_dict() if scope else None,
        "count": count,
        "output": str(args.output),
    }


def scoped_delete_all(runtime, args):
    """Durably delete stored memories and pending work for one scope or user.

    The deletion records an explicit, versioned intent, fences every enumerated
    point and prepared write, invalidates admitted-but-unprocessed turn events
    for the scope, deletes the scope in filtered operations and clears the
    intent only after every target scope is confirmed empty. ``--all-agents``
    covers every agent scope of a user, including the no-agent scope. A literal
    agent id ``'*'`` is an ordinary single scope and never selects all agents;
    a pre-versioned intent that cannot distinguish the two is refused until
    ``--resolve-legacy`` disambiguates it.
    """
    store, ledger = runtime.store, runtime.ledger
    all_agents = bool(getattr(args, "all_agents", False))
    agent = getattr(args, "agent", None)
    user = getattr(args, "user", None)
    resolve_legacy = getattr(args, "resolve_legacy", None)
    if all_agents and agent is not None:
        raise ScopeSelectionError("--all-agents cannot be combined with --agent")
    if resolve_legacy and (all_agents or agent is not None):
        raise ScopeSelectionError(
            "--resolve-legacy cannot be combined with --agent or --all-agents"
        )
    if resolve_legacy and getattr(args, "dry_run", False):
        raise ScopeSelectionError("--resolve-legacy cannot be combined with --dry-run")
    if agent is not None and not user:
        raise ScopeSelectionError("--agent requires --user; scopes are user-anchored")
    pending = pending_scope_deletes(ledger)
    resumed = False
    intent_time = None
    if pending:
        if len(pending) > 1:
            raise ScopeDeleteRefusedError()
        try:
            recorded = json.loads(pending[0]["scope_json"] or "{}")
        except ValueError:
            recorded = {}
        if not isinstance(recorded, dict) or (
            recorded.get("schema_version") != INTENT_SCHEMA_VERSION
        ):
            # Pre-versioned intents cannot distinguish a single-agent deletion
            # of the literal agent '*' from an all-agents deletion. Never
            # guess; the operator disambiguates explicitly.
            if resolve_legacy is None:
                user_value = recorded.get("user_id") if isinstance(recorded, dict) else None
                raise ScopeDeleteLegacyIntentError(
                    user_value if isinstance(user_value, str) else ""
                )
            if not user:
                raise ScopeSelectionError("--resolve-legacy requires --user")
            if not isinstance(recorded, dict) or recorded.get("user_id") != user:
                raise ScopeDeleteRefusedError()
            if not args.confirm:
                raise DeleteConfirmationRequiredError()
            if resolve_legacy == "all_agents":
                all_agents, agent = True, None
            else:
                all_agents = False
                agent = recorded.get("agent_id")
            intent_value = scope_delete_intent(user, agent, all_agents)
            intent_time = resolve_legacy_scope_delete(ledger, pending[0]["scope"], intent_value)
        else:
            recorded_all, recorded_user, recorded_agent = parse_scope_delete_intent(recorded)
            if (user is not None or all_agents) and (
                (user is not None and recorded_user != user)
                or recorded_all != all_agents
                or (user is not None and not recorded_all and recorded_agent != agent)
            ):
                raise ScopeDeleteRefusedError()
            user, all_agents, agent = recorded_user, recorded_all, recorded_agent
            intent_value = scope_delete_intent(user, agent, all_agents)
        resumed = True
    elif resolve_legacy:
        raise ScopeSelectionError("--resolve-legacy requires a pending legacy intent")
    elif user is None:
        if all_agents:
            raise ScopeSelectionError("--all-agents requires --user; scopes are user-anchored")
        raise ScopeDeleteRefusedError()
    else:
        intent_value = scope_delete_intent(user, agent, all_agents)
    identity = store.client.retrieve(store.collection, ids=[IDENTITY_ID], with_payload=True)
    if not identity or not identity[0].payload.get("_qdrant_memory_schema"):
        raise CollectionCompatibilityError(
            "Collection has no trusted qdrant-memory schema identity; refusing scoped deletion"
        )
    result = {
        "collection": store.collection,
        "scope": intent_value,
        "resumed": resumed,
    }
    target_agents = _agents_with_work(store, ledger, user) if all_agents else None
    if args.dry_run:
        agents = target_agents if target_agents is not None else [agent]
        counts = [
            {"agent_id": agent_key, "count": store.count(Scope(user, agent_key))}
            for agent_key in agents
        ]
        result.update(
            dry_run=True,
            would_delete=sum(item["count"] for item in counts),
            scopes=counts,
        )
        return result
    if not args.confirm:
        raise DeleteConfirmationRequiredError()
    if target_agents is None:
        target_agents = [agent]
    if intent_time is None:
        intent_time = begin_scope_delete(ledger, intent_value)
    scopes = [Scope(user, agent_key) for agent_key in target_agents]
    fenced = {index: set() for index in range(len(scopes))}
    for index, scope in enumerate(scopes):
        for point in store.scroll(scope):
            value = point.payload or {}
            operation_key = ledger.enqueue_operation(
                str(point.id),
                "DELETE",
                {"content_hash": value.get("content_hash", "")},
                source_id=ledger.delete_fence_source(scope),
                source_version=intent_time,
            )
            ledger.finish("operations", operation_key, "COMMITTED")
            fenced[index].add(str(point.id))
    # Prepared writes whose points are not stored yet must be fenced too;
    # otherwise a later recovery could replay them and resurrect memories.
    _fence_prepared_writes(ledger, scopes, fenced, intent_time)
    fenced_total = sum(len(entries) for entries in fenced.values())
    superseded_total = _supersede_scope_events(ledger, scopes)
    deleted_total = 0
    for scope in scopes:
        before = store.count(scope)
        store.delete_scope(scope)
        if store.count(scope):
            raise ScopeDeleteIncompleteError()
        deleted_total += before
    finish_scope_delete(ledger, scope_delete_key(intent_value))
    result.update(
        deleted=deleted_total,
        fenced_operations=fenced_total,
        superseded_events=superseded_total,
        ok=True,
    )
    if not all_agents:
        others = store.scopes_for_user(user)
        others.pop(agent, None)
        if others:
            result["other_agent_scopes"] = others
    return result


def _agents_with_work(store, ledger, user):
    """Agent values with stored memories or pending/failed work for one user.

    ``None`` is the no-agent scope; events keep their admitted scope under the
    payload's ``scope`` key, operations carry it at the payload top level.
    """
    agents = set(store.scopes_for_user(user))
    for status in ("PENDING", "FAILED"):
        for table in ("operations", "events"):
            for row in ledger.iter_rows(table, status):
                try:
                    value = json.loads(row["payload_json"] or "{}")
                except ValueError:
                    continue
                scope_value = value.get("scope") if table == "events" else value
                if not isinstance(scope_value, dict):
                    continue
                if scope_value.get("user_id") != user:
                    continue
                agents.add(scope_value.get("agent_id"))
    return sorted(agents, key=repr)


def _fence_prepared_writes(ledger, scopes, fenced, intent_time):
    """Fence prepared UPSERTs for every target scope in one bounded pass.

    Per-scope rescans of pending work would cost O(scopes x rows) under
    ``--all-agents``; one pass with per-scope dedupe keeps the cost linear in
    the ledger size while the per-scope fence identity stays intact.
    """
    targets = {(scope.user_id, scope.agent_id): index for index, scope in enumerate(scopes)}
    for status in ("PENDING", "FAILED"):
        for row in ledger.iter_rows("operations", status):
            if row["action"] != "UPSERT":
                continue
            try:
                value = json.loads(row["payload_json"] or "{}")
            except ValueError:
                continue
            index = targets.get((value.get("user_id"), value.get("agent_id")))
            if index is None or row["point_id"] in fenced[index]:
                continue
            scope = scopes[index]
            operation_key = ledger.enqueue_operation(
                row["point_id"],
                "DELETE",
                {"content_hash": row["content_hash"] or value.get("content_hash", "")},
                source_id=ledger.delete_fence_source(scope),
                source_version=intent_time,
            )
            ledger.finish("operations", operation_key, "COMMITTED")
            fenced[index].add(row["point_id"])


def _supersede_scope_events(ledger, scopes):
    """Invalidate admitted-but-unprocessed turn events for the deleted scopes.

    Events admitted after the deletion are unaffected: the provider cannot
    start while the deletion intent is pending, and new events can only be
    admitted once it is cleared. One bounded pass covers every target scope.
    """
    targets = {(scope.user_id, scope.agent_id) for scope in scopes}
    superseded = 0
    for status in ("PENDING", "FAILED"):
        for row in ledger.iter_rows("events", status):
            try:
                value = json.loads(row["payload_json"] or "{}")
            except ValueError:
                continue
            scope_value = value.get("scope")
            if not isinstance(scope_value, dict):
                continue
            if (scope_value.get("user_id"), scope_value.get("agent_id")) not in targets:
                continue
            ledger.finish("events", row["event_id"], "SUPERSEDED")
            superseded += 1
    return superseded


def _pending_intent_error(intents):
    """Return the fail-closed error for pending scoped-deletion intents.

    Pre-versioned intents are ambiguous and require explicit operator
    disambiguation; every other pending intent uses the standard resume
    guidance.
    """
    for intent in intents:
        try:
            recorded = json.loads(intent.get("scope_json") or "{}")
        except ValueError:
            recorded = {}
        if not isinstance(recorded, dict) or (
            recorded.get("schema_version") != INTENT_SCHEMA_VERSION
        ):
            user = recorded.get("user_id") if isinstance(recorded, dict) else None
            return ScopeDeleteLegacyIntentError(user if isinstance(user, str) else "")
    return ScopeDeleteRecoveryRequiredError()


def run(args, home=None):
    """Run maintenance, with live stderr diagnostics for migrations only."""
    if args.qdrant_command == "migrate":
        with MigrationProgress(quiet=getattr(args, "quiet", False)) as progress:
            return _run(args, home, progress)
    return _run(args, home)


def _run(args, home=None, progress=None):
    """Run a command with profile-scoped configuration and owned service resources.

    Dry runs validate records without opening a target collection or ledger.
    Other commands close their store, ledger and embedder before returning.
    Configuration, service and verification errors propagate to the CLI boundary.
    """
    home = Path(home or active_home())
    overrides = {}
    collection = getattr(args, "target_collection", None) or getattr(args, "collection", None)
    if collection:
        overrides["qdrant"] = {"collection": collection}
    if getattr(args, "oversize", None):
        overrides["limits"] = {"oversize_policy": args.oversize}
    command = args.qdrant_command
    report(progress, "Loading configuration")
    cfg = load_config(home, overrides, resolve_secrets=command != "status")
    if command == "status":
        # Availability/status is local-only and does not probe external services.
        return local_status(cfg, home)
    records = None
    source_type = source_identifier = checksum = None
    if command == "migrate":
        report(progress, "Reading source")
        records, source_type, source_identifier, checksum = migration_source(args, cfg, home)
        report(progress, "Source loaded", len(records), len(records))
        if args.dry_run:
            report(progress, "Validating source", 0, len(records))
            planned = plan(records, cfg, progress=progress)
            report(progress, "Dry run complete", len(planned), len(planned))
            return {
                "dry_run": True,
                "source_unique_count": len(planned),
                "target_collection": cfg["qdrant"]["collection"],
                "target_ids": list(planned),
            }
    pending_reset = has_pending_reset_file(home, ledger_namespace(cfg))
    if pending_reset and command != "init":
        raise ResetRecoveryRequiredError()
    pending_intents = pending_scope_deletes_file(home, ledger_namespace(cfg))
    if pending_intents and command != "delete-all":
        raise _pending_intent_error(pending_intents)
    with ExitStack() as resources:
        if command in {"init", "migrate", "retry", "delete-all"}:
            report(progress, "Acquiring writer lock")
            lease = WriterLease.for_config(home, cfg)
            resources.callback(lease.close)
        if command in {"list", "export", "delete-all"}:
            # Inventory and scoped deletion never embed; they must work even
            # when the embedding service is unavailable or misconfigured.
            embedder = None
        else:
            report(progress, "Initializing embedding client")
            embedder = build_embedder(None, cfg)
            if hasattr(embedder, "close"):
                resources.callback(embedder.close)
        report(progress, "Connecting to target")
        store = QdrantStore(build_client(cfg), cfg, embedder)
        resources.callback(store.close)
        action = "create"
        ledger = None
        if command == "init" and pending_reset:
            # A prior explicit clear remains authorized until its durable intent clears.
            action = "clear"
        elif command == "init" and store.client.collection_exists(store.collection):
            action = existing_collection_action(args, store.collection)
        if action == "clear":
            ledger = Ledger(home, ledger_namespace(cfg))
            resources.callback(ledger.close)
            if not pending_reset:
                begin_destination_reset(ledger)
            resume_destination_reset(store, ledger)
        elif command in {"list", "export", "delete-all"}:
            # Inventory and scoped deletion never embed; they only require the
            # target collection to exist and stay read-only otherwise.
            if not store.client.collection_exists(store.collection):
                raise CollectionNotInitializedError(
                    "Collection does not exist; run hermes qdrant-memory init"
                )
        else:
            report(progress, "Initializing target collection and probing embedding")
            store.initialize(create=command in {"init", "migrate"})
        if ledger is None:
            ledger = Ledger(home, ledger_namespace(cfg))
            resources.callback(ledger.close)
        runtime = Runtime(cfg, store, ledger)
        result = execute_command(
            args, runtime, (records, source_type, source_identifier, checksum), progress
        )
        if command == "init":
            result.update(collection=store.collection, action=action)
            verification = verify_collection(store)
            result["payload_verification"] = {
                "ok": verification["ok"],
                "checked": verification["checked"],
                "invalid_count": len(verification["invalid_ids"]),
            }
            result["ok"] = result["ok"] and verification["ok"]
            if not verification["ok"]:
                result["message"] = (
                    "Collection contains incompatible memory payloads or vectors. "
                    "Choose another collection or explicitly clear and rebuild it."
                )
        report(progress, "Closing resources")
    if command == "migrate":
        stage = (
            "Migration incomplete; rerun with --resume --retry-failed"
            if result["processed"] != result["source_unique_count"]
            else "Migration completed with superseded records; inspect manifest before a fresh import"
            if result.get("superseded")
            else "Migration complete"
        )
        report(progress, stage, result["processed"], result["source_unique_count"])
    return result


def print_json(value, *, indent=None):
    """Handle a downstream reader closing stdout without a shutdown traceback."""
    try:
        print(json.dumps(value, ensure_ascii=False, indent=indent), flush=True)
        return True
    except BrokenPipeError:
        # Prevent Python's final stdout flush from failing on the same closed pipe.
        descriptor = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(descriptor, sys.stdout.fileno())
        finally:
            os.close(descriptor)
        return False


def main(args):
    """Print a JSON result and return a nonzero exit code on sanitized failures."""
    try:
        result = run(args)
        if not print_json(result, indent=2):
            return 1
        return 0 if result.get("ok", True) else 1
    except Exception as exc:
        error = safe_error(exc)
        if isinstance(exc, MigrationSupersededError):
            error.update(
                code="migration_superseded",
                message="Later user intent superseded migration records. Inspect the manifest. "
                "Resuming this migration will not restore deleted memories. Only start a new "
                "migration without --resume if you explicitly intend to import them again.",
            )
        elif isinstance(exc, MigrationIncompleteError):
            error.update(
                code="migration_incomplete",
                message="Migration incomplete; rerun with --resume --retry-failed. Recover "
                "failed work before reviewing any superseded conflict.",
            )
        elif isinstance(exc, WriterBusyError):
            error.update(
                code="writer_busy",
                message="Another Hermes session or gateway owns the writer lock for this "
                "profile and collection. Stop that runtime before init, migrate or retry, "
                "then rerun the command. For an existing remote collection, doctor, stats "
                "and verify can check readiness while the writer is running.",
            )
        elif isinstance(exc, CollectionNotInitializedError):
            error.update(
                code="collection_not_initialized",
                message="Collection does not exist. Run hermes qdrant-memory init first.",
            )
        elif isinstance(exc, ResetRecoveryRequiredError):
            error.update(
                code="reset_recovery_required",
                message="Rerun hermes qdrant-memory init before using this destination.",
            )
        elif isinstance(exc, DeleteConfirmationRequiredError):
            error.update(
                code="delete_confirmation_required",
                message="Refusing to delete without explicit confirmation. Rerun "
                "delete-all with --confirm to delete the selected scope, or with "
                "--dry-run to preview its count.",
            )
        elif isinstance(exc, ScopeSelectionError):
            error.update(
                code="scope_selection_error",
                message="Invalid scope selection: --agent requires --user; --all-agents "
                "cannot be combined with --agent, and memory scopes are always "
                "user-anchored.",
            )
        elif isinstance(exc, ScopeDeleteRecoveryRequiredError):
            error.update(
                code="scope_delete_recovery_required",
                message="An interrupted scoped deletion is pending. Rerun hermes "
                "qdrant-memory delete-all with --confirm to resume it before using "
                "this destination.",
            )
        elif isinstance(exc, ScopeDeleteLegacyIntentError):
            error.update(
                code="scope_delete_legacy_intent",
                message="A pre-versioned scoped-deletion intent is pending and "
                "cannot be resumed automatically because its recorded form is "
                "ambiguous. No data was modified. Resolve it explicitly with "
                "delete-all --user <user> --resolve-legacy single_agent --confirm "
                "or --resolve-legacy all_agents.",
            )
        elif isinstance(exc, ScopeDeleteRefusedError):
            error.update(
                code="scope_delete_refused",
                message="Scoped deletion refused. A pending scoped deletion must be "
                "resumed with its recorded scope (one at a time), and --user is "
                "required for a new scoped deletion.",
            )
        elif isinstance(exc, ScopeDeleteIncompleteError):
            error.update(
                code="scope_delete_incomplete",
                message="Scoped deletion incomplete: the scope still contains points. "
                "The durable intent remains; rerun delete-all --confirm to resume it.",
            )
        elif isinstance(exc, ExportTargetExistsError):
            error.update(
                code="export_target_exists",
                message="Export target already exists; pass --force to overwrite it.",
            )
        elif isinstance(exc, ExportPublicationUnsupportedError):
            error.update(
                code="export_publication_unsupported",
                message="The export target cannot be written with the atomic no-clobber "
                "guarantee (hard links unavailable or the target is not writable). "
                "Pass --force to write without that guarantee, or choose another target.",
            )
        elif isinstance(exc, InitializationChoiceRequiredError):
            error.update(
                code="existing_collection_choice_required",
                message="Choose use or clear. For noninteractive init, pass --existing use "
                "or --existing clear (deletes all collection data).",
            )
        elif isinstance(exc, CollectionCompatibilityError):
            error.update(
                code="collection_incompatible",
                message="Collection is incompatible: a named dense vector with matching "
                "dimensions, distance and a trusted embedding fingerprint is required. "
                "Choose a different collection, or explicitly clear and rebuild with "
                "hermes qdrant-memory init --existing clear (deletes all collection data).",
            )
        print_json({"error": error})
        return 1


globals()["qdrant-memory_command"] = main
