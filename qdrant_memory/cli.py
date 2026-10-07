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
from .ledger import Ledger
from .migration import (
    migrate,
    plan,
    read_json_records,
    read_qdrant_records,
    source_client,
    source_settings,
    verify_collection,
    verify_manifest,
)
from .ownership import WriterBusyError, WriterLease
from .qdrant_store import (
    INDEXES,
    CollectionCompatibilityError,
    CollectionNotInitializedError,
    QdrantStore,
    build_client,
)
from .retry import safe_error
from .runtime import Runtime


class InitializationChoiceRequiredError(ValueError):
    """Require an explicit existing-collection choice outside a terminal."""


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
    parser.set_defaults(func=main)


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


def execute_command(args, runtime, source):
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
            )
        )
    if command == "verify":
        return verify_target(runtime.store, runtime.ledger)
    if command == "retry":
        # Raw events require trusted host LLM calls on the next provider startup.
        runtime.ledger.retry_failed()
        runtime.commit([r["idempotency_key"] for r in runtime.ledger.rows("operations")])
    return diagnostic_status(runtime.store, runtime.ledger, runtime.cfg, command)


def run(args, home=None):
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
    cfg = load_config(home, overrides, resolve_secrets=command != "status")
    if command == "status":
        # Availability/status is local-only and does not probe external services.
        return local_status(cfg, home)
    records = None
    source_type = source_identifier = checksum = None
    if command == "migrate":
        records, source_type, source_identifier, checksum = migration_source(args, cfg, home)
        if args.dry_run:
            planned = plan(records, cfg)
            return {
                "dry_run": True,
                "source_unique_count": len(planned),
                "target_collection": cfg["qdrant"]["collection"],
                "target_ids": list(planned),
            }
    with ExitStack() as resources:
        if command in {"init", "migrate", "retry"}:
            lease = WriterLease.for_config(home, cfg)
            resources.callback(lease.close)
        embedder = build_embedder(None, cfg)
        if hasattr(embedder, "close"):
            resources.callback(embedder.close)
        store = QdrantStore(build_client(cfg), cfg, embedder)
        resources.callback(store.close)
        action = "create"
        ledger = None
        if command == "init" and store.client.collection_exists(store.collection):
            action = existing_collection_action(args, store.collection)
        if action == "clear":
            ledger = Ledger(home, ledger_namespace(cfg))
            resources.callback(ledger.close)
            store.initialize(reset=True, before_reset=ledger.clear_destination)
        else:
            store.initialize(create=command in {"init", "migrate"})
        if ledger is None:
            ledger = Ledger(home, ledger_namespace(cfg))
            resources.callback(ledger.close)
        runtime = Runtime(cfg, store, ledger)
        result = execute_command(args, runtime, (records, source_type, source_identifier, checksum))
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
        if isinstance(exc, WriterBusyError):
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
