"""Operator-only Codex portability orchestration over Runtime and its ledger."""

from . import codex_memory
from .models import Scope


def run(args):
    """Keep preview destination-free; write only after explicit operator approval."""
    from .migration import migrate, plan
    from .standalone import _operator_path, load_standalone_config, open_runtime

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
            report.update(
                migration=manifest, embedding_fingerprint=runtime.store.embedder.fingerprint
            )
            outcomes = []
            for identifier, record in manifest["records"].items():
                row = (
                    runtime.ledger.row("operations", record["operation_key"])
                    if record["operation_key"]
                    else None
                )
                state = (
                    row["status"]
                    if row
                    else ("COMMITTED" if not record["operation_key"] else "MISSING")
                )
                status = (
                    ("SKIPPED" if record["action"] == "SKIP" else "IMPORTED")
                    if state == "COMMITTED"
                    else ("CONFLICT" if state == "SUPERSEDED" else "FAILED")
                )
                outcomes.append(
                    {
                        "target_id": identifier,
                        "status": status,
                        "content_hash": record["content_hash"],
                    }
                )
            report["target_records"] = outcomes
            report["ok"] = all(r["status"] in {"IMPORTED", "SKIPPED"} for r in outcomes)
            return report
    if args.output is None or not args.confirm:
        raise ValueError("Handoff requires a new --output directory and --confirm")
    output = _operator_path(args.output, reject_symlinks=True)
    # Inventory does not need embedding or raw-event recovery. Reuse the
    # operator inventory path, which enforces scope, ownership and storage rules.
    from contextlib import ExitStack

    from .config import ledger_namespace
    from .ownership import WriterLease
    from .qdrant_store import QdrantStore, build_client
    from .reset import has_pending_reset_file, pending_scope_deletes_file

    cfg = load_standalone_config(args.home)
    namespace = ledger_namespace(cfg)
    with ExitStack() as resources:
        lease = WriterLease.for_config(args.home, cfg)
        resources.callback(lease.close)
        if has_pending_reset_file(args.home, namespace) or pending_scope_deletes_file(
            args.home, namespace
        ):
            raise ValueError("Complete pending recovery before creating a handoff")
        store = QdrantStore(build_client(cfg), cfg, None)
        resources.callback(store.close)
        if not store.client.collection_exists(store.collection):
            raise ValueError("Initialize the profile before exporting")
        return codex_memory.write_handoff(output, store.collection, store.scroll(scope), scope)
