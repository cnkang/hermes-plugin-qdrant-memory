"""Hermes CLI commands. No background workers or LLM calls in maintenance mode."""

import hashlib
import json
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
from .ownership import WriterLease
from .qdrant_store import INDEXES, QdrantStore, build_client
from .retry import safe_error
from .runtime import Runtime


def register_cli(subparser):
    """Register maintenance and migration subcommands without starting a provider."""
    commands = subparser.add_subparsers(dest="qdrant_command", required=True)
    for name in ("status", "stats", "doctor", "verify", "retry"):
        parser = commands.add_parser(name)
        parser.add_argument("--collection")
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
    if command == "doctor":
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
        if command in {"migrate", "retry"}:
            lease = WriterLease.for_config(home, cfg)
            resources.callback(lease.close)
        embedder = build_embedder(None, cfg)
        if hasattr(embedder, "close"):
            resources.callback(embedder.close)
        store = QdrantStore(build_client(cfg), cfg, embedder)
        resources.callback(store.close)
        store.initialize(create=command == "migrate")
        ledger = Ledger(home, ledger_namespace(cfg))
        resources.callback(ledger.close)
        runtime = Runtime(cfg, store, ledger)
        return execute_command(args, runtime, (records, source_type, source_identifier, checksum))


def main(args):
    """Print a JSON result and return a nonzero exit code on sanitized failures."""
    try:
        result = run(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", True) else 1
    except Exception as exc:
        print(json.dumps({"error": safe_error(exc)}))
        return 1


globals()["qdrant-memory_command"] = main
