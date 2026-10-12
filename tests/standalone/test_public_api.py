"""In-process public boundaries complement real installed CLI/MCP acceptance."""

import asyncio
import json

import pytest

from qdrant_memory import mcp_server, standalone
from qdrant_memory.config import ledger_namespace
from qdrant_memory.ledger import Ledger
from qdrant_memory.models import Scope, payload, point_id
from qdrant_memory.ownership import WriterLease
from qdrant_memory.reset import begin_destination_reset, begin_scope_delete, scope_delete_intent
from qdrant_memory.standalone import (
    OperatorPathOutsideRootError,
    call_tool,
    load_standalone_config,
    open_runtime,
    validate_operator_paths,
)

from .test_standalone import command
from .test_standalone import profile as standalone_profile


@pytest.fixture
def profile(tmp_path):
    """Reuse the isolated HTTP/storage profile with normal generator cleanup."""
    yield from standalone_profile.__wrapped__(tmp_path)


@pytest.fixture(autouse=True)
def operator_test_environment(monkeypatch):
    """Keep in-process acceptance independent of developer service credentials."""
    for key in ("QDRANT_URL", "QDRANT_API_KEY", "EMBEDDING_API_KEY"):
        monkeypatch.delenv(key, raising=False)


def test_public_cli_json_crud_and_failure_boundaries(profile, monkeypatch, capsys):
    """Public main keeps CRUD durable and serializes sanitized failures correctly."""
    home, _ = profile
    monkeypatch.chdir(home)
    args = ["--home", str(home)]
    assert standalone.main([*args, "status"]) == 0
    assert json.loads(capsys.readouterr().out)["collection"] == "qdrant_memory"
    assert standalone.main([*args, "init"]) == 0
    capsys.readouterr()
    assert standalone.main([*args, "add", "--text", "Public API durable fact"]) == 0
    identifier = json.loads(capsys.readouterr().out)["id"]
    assert (
        standalone.main(
            [*args, "search", "--query", "Public API durable fact", "--category", "fact"]
        )
        == 0
    )
    assert "memories" in json.loads(capsys.readouterr().out)
    assert standalone.main([*args, "update", "--id", identifier, "--text", "Replacement fact"]) == 0
    capsys.readouterr()
    assert standalone.main([*args, "delete", "--id", identifier]) == 0
    capsys.readouterr()
    assert standalone.main([*args, "delete", "--id", identifier]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["error"] == json.loads(output.err)["error"]
    assert identifier not in output.out
    monkeypatch.setattr(standalone.cli, "print_json", lambda result: False)
    assert standalone.main([*args, "status"]) == 1


def test_cli_nonready_and_mcp_startup_output(profile, monkeypatch, capsys):
    """Readiness failures return nonzero and MCP startup keeps stdout protocol-only."""
    home, _ = profile
    monkeypatch.chdir(home)
    monkeypatch.setattr(standalone.cli, "run", lambda *args, **kwargs: {"ok": False})
    assert standalone.main(["--home", str(home), "status"]) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False}
    with pytest.raises(SystemExit) as error:
        standalone.main(["--home", str(home), "add"])
    assert error.value.code == 2
    capsys.readouterr()
    assert standalone.main(["--home", str(home), "mcp"]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)["error"]["type"] == "CollectionNotInitializedError"
    cfg = load_standalone_config(home)
    # Failed MCP startup released its lease despite the service initialization error.
    lease = WriterLease.for_config(home, cfg)
    lease.close()


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("qdrant_memory_migrate", {}),
        ("qdrant_memory_add", []),
        ("qdrant_memory_add", {"text": "bad", "source_json": "private-source.json"}),
        ("qdrant_memory_add", {}),
        ("qdrant_memory_add", {"text": {"secret": "must-not-echo"}}),
    ],
)
def test_public_schema_denial_never_admits_work(profile, name, arguments):
    """Unknown tools, filesystem options and malformed values cannot reach the ledger."""
    command(profile, "init")
    with open_runtime(profile[0], secret_reader=lambda name: None) as runtime:
        before = runtime.ledger.stats()
        with pytest.raises(ValueError):
            call_tool(runtime, name, arguments)
        assert runtime.ledger.stats() == before


@pytest.mark.parametrize("intent", ["reset", "scope-delete", "raw-event"])
def test_public_startup_guards_release_owned_resources(profile, intent):
    """Destructive or raw-event recovery blocks startup and releases its writer lease."""
    home, _ = profile
    command(profile, "init")
    cfg = load_standalone_config(home)
    ledger = Ledger(home, ledger_namespace(cfg))
    if intent == "reset":
        begin_destination_reset(ledger)
    elif intent == "scope-delete":
        begin_scope_delete(ledger, scope_delete_intent("operator", "agent", False))
    else:
        ledger.enqueue_event({"session_id": "raw-host", "user": "trusted host only"})
    ledger.close()
    with pytest.raises((RuntimeError, ValueError)):
        with open_runtime(home):
            pytest.fail("Startup admitted tools before recovery")
    lease = WriterLease.for_config(home, cfg)
    lease.close()


def test_recovery_failure_closes_store_embedder_and_lease(profile, monkeypatch):
    """A failed prepared mutation remains durable and startup cleanup frees ownership."""
    home, _ = profile
    path = home / "qdrant-memory.json"
    cfg = json.loads(path.read_text())
    cfg["embedding"]["document_instruction"] = "doc:"
    cfg["write"] = {"max_attempts": 2, "backoff_base_seconds": 0, "backoff_max_seconds": 0}
    path.write_text(json.dumps(cfg))
    command(profile, "init")
    cfg = load_standalone_config(home)
    ledger = Ledger(home, ledger_namespace(cfg))
    scope = Scope(**cfg["scope"])
    value = payload("restart prepared write", scope, "manual_tool", "prepared")
    identifier = point_id(scope, "manual_tool", value["content_hash"])
    key = ledger.enqueue_operation(identifier, "UPSERT", value)
    ledger.close()
    create_embedder = standalone.build_embedder
    embedders = []

    def tracked_embedder(*args, **kwargs):
        """Track actual owned HTTP clients through failed startup cleanup."""
        embedder = create_embedder(*args, **kwargs)
        embedders.append(embedder)
        return embedder

    monkeypatch.setattr(standalone, "build_embedder", tracked_embedder)
    with pytest.raises(RuntimeError, match="require recovery"):
        with open_runtime(home):
            pytest.fail("Recovery failure admitted new tools")
    assert embedders[0].client.is_closed
    lease = WriterLease.for_config(home, cfg)
    lease.close()
    ledger = Ledger(home, ledger_namespace(cfg))
    assert ledger.row("operations", key)["status"] == "FAILED"
    ledger.close()
    # Real embedded storage can reopen immediately; failed startup closed its client.
    command(profile, "retry")
    with open_runtime(home) as runtime:
        assert runtime.store.get(identifier, scope) is not None


def test_official_inprocess_client_scoped_tools_and_path_denial(profile):
    """Real SDK memory transport reaches production handlers and rejects file arguments."""
    from mcp.shared.memory import create_connected_server_and_client_session

    command(profile, "init")
    with open_runtime(profile[0]) as runtime:
        server = mcp_server.create_server(runtime)

        async def check():
            """Exercise initialization, shared tool schemas, CRUD and sanitized errors."""
            async with create_connected_server_and_client_session(server) as client:
                tools = (await client.list_tools()).tools
                assert len(tools) == 4
                result = await client.call_tool("qdrant_memory_add", {"text": "SDK memory fact"})
                identifier = json.loads(result.content[0].text)["id"]
                assert not result.isError
                assert not (
                    await client.call_tool("qdrant_memory_search", {"query": "SDK memory fact"})
                ).isError
                assert not (
                    await client.call_tool(
                        "qdrant_memory_update", {"id": identifier, "text": "SDK update"}
                    )
                ).isError
                for name, arguments in [
                    ("qdrant_memory_migrate", {"source_json": "/private/source.json"}),
                    ("qdrant_memory_add", {"text": "bad", "source_json": "/private/source.json"}),
                    ("qdrant_memory_search", {"query": "bad", "output": "/private/source.json"}),
                    ("qdrant_memory_add", {"text": {"secret": "must-not-echo"}}),
                ]:
                    result = await client.call_tool(name, arguments)
                    assert result.isError
                    assert "private/source.json" not in result.content[0].text
                    assert "must-not-echo" not in result.content[0].text
                assert not (
                    await client.call_tool("qdrant_memory_delete", {"id": identifier})
                ).isError

        asyncio.run(check())


@pytest.mark.parametrize(
    "option,command_args",
    [
        ("--source-json", ["migrate", "mem0", "--dry-run"]),
        (
            "--source-config",
            ["migrate", "mem0", "--source-qdrant-collection", "legacy", "--dry-run"],
        ),
        ("--output", ["export"]),
    ],
)
def test_cli_path_escape_refused_before_content_or_storage_io(
    tmp_path, monkeypatch, capsys, option, command_args
):
    """Explicit file options cannot leave operator cwd and never reach maintenance services."""
    cwd = tmp_path / "operator"
    cwd.mkdir()
    home = tmp_path / "fresh-profile"
    outside = tmp_path / "private.json"
    outside.write_text("do-not-read")
    monkeypatch.chdir(cwd)

    def unexpected(*args, **kwargs):
        """Trap any maintenance invocation before the path authorization boundary."""
        pytest.fail("Out-of-root path reached maintenance")

    monkeypatch.setattr(standalone.cli, "run", unexpected)
    assert standalone.main(["--home", str(home), *command_args, option, str(outside)]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "OperatorPathOutsideRootError"
    assert not home.exists()
    assert outside.read_text() == "do-not-read"


def test_canonical_path_containment_and_authorized_external_profile_source(
    tmp_path, monkeypatch, capsys
):
    """Cwd authorization supports outside-profile sources and rejects symlink traversal."""
    cwd = tmp_path / "authorized-sources"
    cwd.mkdir()
    home = tmp_path / "separate-profile"
    outside = tmp_path / "private.json"
    outside.write_text("private")
    source = cwd / "source.json"
    source.write_text(
        json.dumps([{"id": "legacy", "memory": "Authorized source fact", "user_id": "operator"}])
    )
    monkeypatch.chdir(cwd)
    args = standalone.parser().parse_args(
        ["--home", str(home), "migrate", "mem0", "--source-json", "source.json", "--dry-run"]
    )
    validate_operator_paths(args)
    assert args.source_json == source.resolve()
    assert (
        standalone.main(
            [
                "--home",
                str(home),
                "migrate",
                "mem0",
                "--source-json",
                str(source),
                "--dry-run",
                "--quiet",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["source_unique_count"] == 1
    assert not home.exists()
    link = cwd / "linked.json"
    try:
        link.symlink_to(outside)
    except OSError:
        # Windows may deny native symlinks; traversal containment is still required.
        link = cwd / ".." / "private.json"
    args.source_json = link
    with pytest.raises(OperatorPathOutsideRootError):
        validate_operator_paths(args)
    assert outside.read_text() == "private"


def test_public_mcp_main_uses_official_protocol_and_closes_session(profile, monkeypatch, capsys):
    """Exercise the synchronous stdio entry boundary with an official in-process client."""
    from contextlib import asynccontextmanager

    import anyio
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    home, _ = profile
    command(profile, "init")

    @asynccontextmanager
    async def transport():
        """Substitute transport streams while retaining both real SDK protocol loops."""
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            async with anyio.create_task_group() as tasks:

                async def client():
                    """Complete a real initialization and tool call, then signal stream EOF."""
                    async with ClientSession(*client_streams) as session:
                        await session.initialize()
                        result = await session.call_tool(
                            "qdrant_memory_add", {"text": "Entry boundary fact"}
                        )
                        assert not result.isError
                    await client_streams[1].aclose()

                tasks.start_soon(client)
                yield server_streams

    monkeypatch.setattr("mcp.server.stdio.stdio_server", transport)
    assert standalone.main(["--home", str(home), "mcp"]) == 0
    assert not capsys.readouterr().out
    with open_runtime(home) as runtime:
        assert runtime.store.count(Scope(**runtime.cfg["scope"])) == 1


@pytest.mark.parametrize("intent", ["reset", "scope-delete"])
def test_destructive_intent_admitted_during_lease_acquisition_is_rechecked(
    profile, monkeypatch, intent
):
    """A raced intent cannot bypass startup's post-ownership guard or open a service."""
    home, _ = profile
    command(profile, "init")
    acquire = WriterLease.for_config
    held = []

    def acquire_and_admit(home, cfg):
        """Acquire the actual OS lease, then simulate a durable intent arriving in the gap."""
        lease = acquire(home, cfg)
        held.append(lease)
        ledger = Ledger(home, ledger_namespace(cfg))
        if intent == "reset":
            begin_destination_reset(ledger)
        else:
            begin_scope_delete(ledger, scope_delete_intent("operator", "agent", False))
        ledger.close()
        return lease

    monkeypatch.setattr(WriterLease, "for_config", acquire_and_admit)
    monkeypatch.setattr(
        standalone,
        "build_embedder",
        lambda *args, **kwargs: pytest.fail("Post-lease guard opened embedding service"),
    )
    with pytest.raises((RuntimeError, ValueError)):
        with open_runtime(home):
            pytest.fail("Raced intent admitted tools")
    assert held[0].file.closed
