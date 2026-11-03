"""Operator-only Codex portability orchestration over Runtime and its ledger."""

from pathlib import Path

from . import codex_memory
from .models import Scope


def _target_records(runtime, manifest):
    """Report each migration target's actual durable operation state."""
    from .migration import _record_status

    outcomes = []
    for identifier, record in manifest["records"].items():
        key = record["operation_key"]
        row = runtime.ledger.row("operations", key) if key else None
        state = _record_status(record, row)
        if state == "COMMITTED":
            status = "SKIPPED" if record["action"] == "SKIP" else "IMPORTED"
        elif state == "SUPERSEDED":
            status = "CONFLICT"
        else:
            status = "FAILED"
        outcomes.append(
            {"target_id": identifier, "status": status, "content_hash": record["content_hash"]}
        )
    return outcomes


def _migrate(args, cfg, scope):
    """Preview first, then open the writer only for a confirmed import."""
    from .migration import migrate, plan
    from .standalone import open_runtime

    records, report = codex_memory.read_source(
        args.codex_home,
        versions=args.source_version,
        scope=scope,
        headings=getattr(args, "heading", None),
    )
    planned = plan(records, cfg, source_type="codex-native")
    report.update(dry_run=args.dry_run, target_ids=list(planned))
    if args.dry_run:
        return report
    if not args.confirm:
        raise ValueError("Review a dry run, then authorize import with --confirm")
    with open_runtime(args.home) as runtime:
        manifest = migrate(
            runtime,
            records,
            "codex-native",
            report["source_id"],
            report["source_sha256"],
            args.resume,
            args.retry_failed,
            args.verify,
        )
        report.update(migration=manifest, embedding_fingerprint=runtime.store.embedder.fingerprint)
        outcomes = _target_records(runtime, manifest)
        report["target_records"] = outcomes
        report["ok"] = all(r["status"] in {"IMPORTED", "SKIPPED"} for r in outcomes)
        return report


def _export(args, scope):
    """Export under the existing operator inventory ownership rules."""
    # Inventory does not need embedding or raw-event recovery. Reuse the
    # operator inventory path, which enforces scope, ownership and storage rules.
    from contextlib import ExitStack

    from .config import ledger_namespace
    from .ownership import WriterLease
    from .qdrant_store import QdrantStore, build_client
    from .reset import has_pending_reset_file, pending_scope_deletes_file
    from .standalone import _operator_path, load_standalone_config

    if args.output is None or not args.confirm:
        raise ValueError("Handoff requires a new --output directory and --confirm")
    output = _operator_path(args.output, reject_symlinks=True)
    home = Path(args.home).expanduser().resolve()
    cfg = load_standalone_config(home)
    namespace = ledger_namespace(cfg)
    with ExitStack() as resources:
        lease = WriterLease.for_config(home, cfg)
        resources.callback(lease.close)
        if has_pending_reset_file(home, namespace) or pending_scope_deletes_file(home, namespace):
            raise ValueError("Complete pending recovery before creating a handoff")
        store = QdrantStore(build_client(cfg), cfg, None)
        resources.callback(store.close)
        if not store.client.collection_exists(store.collection):
            raise ValueError("Initialize the profile before exporting")
        return codex_memory.write_handoff(output, store.collection, store.scroll(scope), scope)


def run(args):
    """Keep preview destination-free; write only after explicit operator approval."""
    from .standalone import _operator_path, load_standalone_config

    if args.action == "inspect":
        _, report = codex_memory.read_source(
            args.codex_home, versions=args.source_version, headings=getattr(args, "heading", None)
        )
        return report
    if args.action == "verify":
        if args.output is None:
            raise ValueError("Select a handoff directory with --output")
        return codex_memory.verify_handoff(_operator_path(args.output, reject_symlinks=True))
    cfg = load_standalone_config(args.home, resolve_secrets=False)
    scope = Scope(**cfg["scope"])
    if args.action == "migrate":
        return _migrate(args, cfg, scope)
    return _export(args, scope)
