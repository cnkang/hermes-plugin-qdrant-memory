"""Supported host-independent configuration, runtime and JSON console interface."""

import argparse
import json
import os
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path

from . import cli
from .config import DEFAULTS, embedding_config, ledger_namespace, load_config, merge, validate_url
from .embedding import build_embedder
from .ledger import Ledger
from .models import Scope
from .ownership import WriterLease
from .qdrant_store import QdrantStore, build_client
from .reset import (
    ResetRecoveryRequiredError,
    has_pending_reset_file,
    pending_scope_deletes_file,
)
from .retry import safe_error
from .runtime import Runtime
from .tools import dispatch, schemas

STANDALONE_DEFAULTS = merge(
    DEFAULTS,
    {
        "scope": {"user_id": "local-user", "agent_id": None},
        "qdrant": {"collection": "qdrant_memory"},
    },
)


class HostExtractionRequiredError(RuntimeError):
    """Require the trusted Hermes extractor to recover admitted raw turn events."""


def default_home():
    """Return a host-neutral home without consulting Hermes environment settings."""
    return Path.home() / ".qdrant-memory"


def load_standalone_config(home, overrides=None, resolve_secrets=True, *, secret_reader=None):
    """Read strict JSON and trusted operator environment secrets before storage I/O."""
    reader = os.environ.get if secret_reader is None else secret_reader
    cfg = load_config(
        home,
        overrides,
        resolve_secrets,
        secret_reader=reader,
        json_reader=lambda path: json.loads(path.read_text(encoding="utf-8")),
        defaults=STANDALONE_DEFAULTS,
    )
    if resolve_secrets:
        emb = embedding_config(cfg)
        key = emb.get("api_key") or reader(emb.get("api_key_env", "EMBEDDING_API_KEY"))
        validate_url(emb["base_url"], api_key=key)
    return cfg


@contextmanager
def open_runtime(home=None, *, secret_reader=None):
    """Own one fixed-scope writer session using the existing durable core.

    A session requires an initialized compatible collection. Pending bulk reset
    or deletion intents fail closed and must be resumed with the operator CLI.
    The lease outlives the store, ledger and embedder, including startup failure.
    """
    home = Path(home or default_home()).expanduser().resolve()
    reader = os.environ.get if secret_reader is None else secret_reader
    cfg = load_standalone_config(home, secret_reader=reader)
    namespace = ledger_namespace(cfg)
    if has_pending_reset_file(home, namespace):
        raise ResetRecoveryRequiredError()
    intents = pending_scope_deletes_file(home, namespace)
    if intents:
        raise cli._pending_intent_error(intents)
    with ExitStack() as resources:
        lease = WriterLease.for_config(home, cfg)
        resources.callback(lease.close)
        # Recheck after ownership: an operator may have started a destructive
        # intent between the initial inspection and acquiring the lease.
        if has_pending_reset_file(home, namespace):
            raise ResetRecoveryRequiredError()
        intents = pending_scope_deletes_file(home, namespace)
        if intents:
            raise cli._pending_intent_error(intents)
        embedder = build_embedder(None, cfg, secret_reader=reader)
        resources.callback(embedder.close)
        store = QdrantStore(build_client(cfg), cfg, embedder)
        resources.callback(store.close)
        store.initialize(create=False)
        ledger = Ledger(home, namespace)
        resources.callback(ledger.close)
        # Raw turns may contain sensitive host context and require its trusted
        # extractor. Never run recover(extractor=None): that would change raw
        # events to FAILED and let a later restart bypass pending work.
        if any(
            next(ledger.iter_rows("events", status), None) is not None
            for status in ("PENDING", "FAILED")
        ):
            raise HostExtractionRequiredError()
        runtime = Runtime(cfg, store, ledger)
        if runtime.recover():
            raise RuntimeError("Pending durable operations require recovery before new tools")
        yield runtime


def call_tool(runtime, name, arguments):
    """Validate the shared schema before dispatch; identities are operator-controlled."""
    definition = next((item for item in schemas() if item["name"] == name), None)
    if definition is None:
        raise ValueError("Unknown memory tool")
    schema = definition["parameters"]
    if not isinstance(arguments, dict) or set(arguments) - set(schema["properties"]):
        raise ValueError("Unexpected memory tool arguments")
    if any(key not in arguments for key in schema["required"]):
        raise ValueError("Missing memory tool arguments")
    if any(not isinstance(value, str) for value in arguments.values()):
        raise ValueError("Memory tool arguments must be strings")
    scope = Scope(runtime.cfg["scope"]["user_id"], runtime.cfg["scope"].get("agent_id"))
    return dispatch(runtime, name, arguments, scope, "standalone")


class OperatorPathOutsideRootError(ValueError):
    """Reject explicit CLI file paths outside the operator invocation directory."""


def validate_operator_paths(args):
    """Contain explicit maintenance file arguments within the trusted invocation cwd.

    Resolve both symlinks and parent segments before any file content or service
    access. Home/storage paths and default profile files remain trusted profile
    configuration; this boundary applies only to explicit source/export options.
    """
    root = Path.cwd().resolve()
    for key in ("source_json", "source_config", "output"):
        value = getattr(args, key, None)
        if value is None:
            continue
        path = Path(value).expanduser().resolve()
        if not path.is_relative_to(root):
            raise OperatorPathOutsideRootError()
        setattr(args, key, path)


def parser():
    """Extend the Hermes maintenance grammar with scoped tools and stdio MCP."""
    result = argparse.ArgumentParser(prog="qdrant-memory")
    result.add_argument("--home", type=Path, default=default_home())
    commands = cli.register_cli(result)
    for definition in schemas():
        name = definition["name"].removeprefix("qdrant_memory_")
        command = commands.add_parser(name, help=definition["description"])
        for key in definition["parameters"]["properties"]:
            command.add_argument("--" + key, required=key in definition["parameters"]["required"])
    commands.add_parser("mcp", help="Serve fixed-scope memory tools over local stdio MCP")
    return result


def main(argv=None):
    """Print JSON results; reserve stdout exclusively for MCP protocol in MCP mode."""
    args = parser().parse_args(argv)
    try:
        if args.qdrant_command == "mcp":
            from .mcp_server import serve

            serve(args.home)
            return 0
        if args.qdrant_command in {"search", "add", "update", "delete"}:
            with open_runtime(args.home) as runtime:
                definition = next(
                    item
                    for item in schemas()
                    if item["name"] == "qdrant_memory_" + args.qdrant_command
                )
                arguments = {
                    key: getattr(args, key)
                    for key in definition["parameters"]["properties"]
                    if getattr(args, key) is not None
                }
                result = call_tool(runtime, definition["name"], arguments)
        else:
            validate_operator_paths(args)
            result = cli.run(
                args, args.home, config_loader=load_standalone_config, secret_reader=os.environ.get
            )
        if not cli.print_json(result):
            return 1
        return 0 if result.get("ok", True) else 1
    except Exception as exc:
        error = safe_error(exc)
        # Error text from services/configuration may contain credentials. Keep the
        # machine-readable diagnostic sanitized just like the Hermes boundary.
        print(json.dumps({"error": error}), file=sys.stderr, flush=True)
        if args.qdrant_command != "mcp":
            cli.print_json({"error": error})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
