"""Real subprocess contracts without any Hermes installation or import path."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import sysconfig
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from qdrant_memory.config import ledger_namespace
from qdrant_memory.ledger import Ledger
from qdrant_memory.ownership import WriterLease
from qdrant_memory.reset import begin_destination_reset, begin_scope_delete, scope_delete_intent
from qdrant_memory.standalone import load_standalone_config


@pytest.fixture
def profile(tmp_path):
    """Run an actual local embedding HTTP service and strict no-Hermes subprocesses."""

    class Handler(BaseHTTPRequestHandler):
        """Return deterministic nonzero embeddings through the real HTTP transport."""

        attempts = {}

        def do_POST(self):
            """Answer Ollama and OpenAI-compatible embedding requests."""
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            for text in body["input"]:
                if text.startswith(("doc:transient", "doc:restart")):
                    self.attempts[text] = self.attempts.get(text, 0) + 1
                    failures = 1 if text.startswith("doc:transient") else 2
                    if self.attempts[text] <= failures:
                        self.send_response(503)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
            vectors = []
            for text in body["input"]:
                if text.startswith("similarity-fixture:"):
                    # Distinct synthetic facts deliberately share a unit vector.
                    vectors.append([1.0, 0.0, 0.0])
                    continue
                values = hashlib.sha256(text.encode()).digest()[:3]
                vectors.append([value / 255 + 0.01 for value in values])
            result = {
                "embeddings": vectors,
                "data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)],
            }
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            """Keep test diagnostics quiet."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / "profile"
    home.mkdir()
    cfg = {
        "scope": {"user_id": "operator", "agent_id": "agent"},
        "embedding": {
            "dimensions": 3,
            "model": "fixture",
            "base_url": f"http://127.0.0.1:{server.server_port}",
        },
    }
    (home / "qdrant-memory.json").write_text(json.dumps(cfg), encoding="utf-8")
    guard = tmp_path / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        "import sys\nclass Guard:\n def find_spec(self, fullname, path=None, target=None):\n  if fullname.split('.')[0] in {'agent', 'hermes_constants', 'hermes_cli', 'utils'}: raise RuntimeError('Hermes import forbidden: ' + fullname)\nsys.meta_path.insert(0, Guard())\n",
        encoding="utf-8",
    )
    # Pass OS runtime necessities only; failures must not render unrelated
    # credentials from the developer's inherited environment in fixture reprs.
    allowed = {
        "PATH",
        "HOME",
        "SYSTEMROOT",
        "WINDIR",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "APPDATA",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LANG",
        "TZ",
        "PYTHONIOENCODING",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["PYTHONPATH"] = str(guard)
    yield home, env
    server.shutdown()
    server.server_close()
    thread.join()


def command(profile, *args, ok=True):
    """Exercise the installed console entry point from a foreign working directory."""
    home, env = profile
    executable = Path(sysconfig.get_path("scripts")) / (
        "qdrant-memory.exe" if os.name == "nt" else "qdrant-memory"
    )
    result = subprocess.run(
        [str(executable), "--home", str(home), *args],
        env=env,
        cwd=home,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) == ok, (result.stdout, result.stderr)
    return json.loads(result.stdout)


def test_cli_crud_restart_scope_and_migration(profile):
    """CRUD is durable; foreign IDs are denied and migration shares the core."""
    home, _ = profile
    assert command(profile, "status")["collection"] == "qdrant_memory"
    assert command(profile, "init")["ok"]
    memory = command(profile, "add", "--text", "Cats prefer warm sunlight")
    memory_id = memory["id"]
    assert (
        command(profile, "search", "--query", "Cats prefer warm sunlight")["memories"][0]["id"]
        == memory_id
    )
    assert (
        command(profile, "update", "--id", memory_id, "--text", "Cats prefer soft blankets")[
            "action"
        ]
        == "UPDATE"
    )
    cfg_path = home / "qdrant-memory.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["scope"]["user_id"] = "foreign"
    cfg_path.write_text(json.dumps(cfg))
    assert not command(profile, "search", "--query", "Cats")["memories"]
    assert "error" in command(profile, "delete", "--id", memory_id, ok=False)
    assert "error" in command(profile, "update", "--id", memory_id, "--text", "bad", ok=False)
    cfg["scope"]["user_id"] = "operator"
    cfg_path.write_text(json.dumps(cfg))
    assert command(profile, "delete", "--id", memory_id)["action"] == "DELETE"
    assert not command(profile, "search", "--query", "Cats")["memories"]
    source = home / "source.json"
    source.write_text(
        json.dumps(
            [
                {
                    "id": "legacy",
                    "memory": "Dogs enjoy walking",
                    "user_id": "operator",
                    "agent_id": "agent",
                }
            ]
        )
    )
    assert command(profile, "migrate", "mem0", "--source-json", str(source), "--dry-run")["dry_run"]
    assert (
        command(profile, "migrate", "mem0", "--source-json", str(source), "--verify", "--quiet")[
            "processed"
        ]
        == 1
    )
    assert command(profile, "list", "--user", "operator", "--agent", "agent")["count"]["total"] == 1
    export = home / "export.json"
    command(profile, "export", "--output", str(export))
    assert "error" in command(profile, "export", "--output", str(export), ok=False)
    assert command(profile, "verify")["ok"]


def test_invalid_configuration_never_creates_storage(profile):
    """Malformed config and unsafe resolved embedding secrets fail before storage I/O."""
    home, env = profile
    (home / "qdrant-memory.json").write_text('{"scope": {"user_id": ""}}')
    command(profile, "add", "--text", "invalid", ok=False)
    assert not (home / "qdrant-memory").exists()
    (home / "qdrant-memory.json").write_text('{"embedding": {"base_url": "http://example.com"}}')
    env["EMBEDDING_API_KEY"] = "private-fixture"
    command(profile, "init", ok=False)
    assert not (home / "qdrant-memory").exists()
    (home / "qdrant-memory.json").write_text("{broken")
    command(profile, "init", ok=False)
    assert not (home / "qdrant-memory").exists()


def test_writer_and_destructive_recovery(profile):
    """All explicit tools honor lease exclusion and pending destructive intents."""
    home, _ = profile
    command(profile, "init")
    cfg = load_standalone_config(home)
    lease = WriterLease.for_config(home, cfg)
    try:
        assert (
            command(profile, "add", "--text", "busy", ok=False)["error"]["type"]
            == "WriterBusyError"
        )
    finally:
        lease.close()
    command(profile, "add", "--text", "durable before deletion")
    ledger = Ledger(home, ledger_namespace(cfg))
    begin_scope_delete(ledger, scope_delete_intent("operator", "agent", False))
    ledger.close()
    assert (
        command(profile, "add", "--text", "blocked", ok=False)["error"]["type"]
        == "ScopeDeleteRecoveryRequiredError"
    )
    command(profile, "delete-all", "--confirm")
    assert not command(profile, "search", "--query", "durable")["memories"]
    ledger = Ledger(home, ledger_namespace(cfg))
    begin_destination_reset(ledger)
    ledger.close()
    assert (
        command(profile, "add", "--text", "blocked", ok=False)["error"]["type"]
        == "ResetRecoveryRequiredError"
    )
    command(profile, "init")
    command(profile, "add", "--text", "works after reset")


def test_official_mcp_client(profile):
    """Initialize/list/call use the official client and a real installed server process."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    home, env = profile
    command(profile, "init")
    config_path = home / "qdrant-memory.json"
    cfg = json.loads(config_path.read_text())
    cfg["scope"]["agent_id"] = "foreign-agent"
    config_path.write_text(json.dumps(cfg))
    foreign_id = command(profile, "add", "--text", "Foreign MCP memory")["id"]
    cfg["scope"]["agent_id"] = "agent"
    config_path.write_text(json.dumps(cfg))

    async def check():
        """Verify stable tool schemas, mutations and identity-override rejection."""
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "qdrant_memory.standalone", "--home", str(home), "mcp"],
            env=env,
        )
        async with stdio_client(parameters, errlog=sys.stderr) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                assert {tool.name for tool in tools} == {
                    "qdrant_memory_search",
                    "qdrant_memory_add",
                    "qdrant_memory_update",
                    "qdrant_memory_delete",
                }
                assert all(
                    tool.model_dump(by_alias=True)["inputSchema"]["additionalProperties"] is False
                    for tool in tools
                )
                added = await session.call_tool("qdrant_memory_add", {"text": "MCP durable fact"})
                assert not added.model_dump(by_alias=True)["isError"]
                memory_id = json.loads(added.content[0].text)["id"]
                assert not (
                    await session.call_tool("qdrant_memory_search", {"query": "MCP durable fact"})
                ).model_dump(by_alias=True)["isError"]
                assert not (
                    await session.call_tool(
                        "qdrant_memory_update", {"id": memory_id, "text": "MCP replacement"}
                    )
                ).model_dump(by_alias=True)["isError"]
                assert (
                    await session.call_tool(
                        "qdrant_memory_add", {"text": "bad", "user_id": "foreign"}
                    )
                ).model_dump(by_alias=True)["isError"]
                assert (
                    await session.call_tool(
                        "qdrant_memory_update",
                        {"id": "00000000-0000-0000-0000-000000000001", "text": "bad"},
                    )
                ).model_dump(by_alias=True)["isError"]
                assert (
                    await session.call_tool("qdrant_memory_delete", {"id": foreign_id})
                ).model_dump(by_alias=True)["isError"]
                malformed = await session.call_tool("qdrant_memory_add", {"text": 123})
                assert malformed.model_dump(by_alias=True)["isError"]
                assert "123" not in malformed.content[0].text
                assert (await session.call_tool("qdrant_memory_add", {})).model_dump(by_alias=True)[
                    "isError"
                ]
                assert (await session.call_tool("qdrant_memory_delete_all", {})).model_dump(
                    by_alias=True
                )["isError"]
                assert not (
                    await session.call_tool("qdrant_memory_delete", {"id": memory_id})
                ).model_dump(by_alias=True)["isError"]
                assert (
                    command(profile, "add", "--text", "busy", ok=False)["error"]["type"]
                    == "WriterBusyError"
                )

    asyncio.run(check())
    assert not command(profile, "search", "--query", "MCP replacement")["memories"]


def test_transport_retry_and_durable_failed_write(profile):
    """Retry transient transport failures and replay failed durable writes on restart."""
    home, _ = profile
    path = home / "qdrant-memory.json"
    cfg = json.loads(path.read_text())
    cfg["embedding"]["document_instruction"] = "doc:"
    cfg["write"] = {"max_attempts": 2, "backoff_base_seconds": 0, "backoff_max_seconds": 0}
    path.write_text(json.dumps(cfg))
    command(profile, "init")
    assert command(profile, "add", "--text", "transient one-time failure")["action"] == "ADD"
    assert command(profile, "add", "--text", "restart durable write", ok=False)["error"][
        "retryable"
    ]
    runtime_cfg = load_standalone_config(home)
    ledger = Ledger(home, ledger_namespace(runtime_cfg))
    try:
        assert ledger.stats()["operations"]["FAILED"] == 1
    finally:
        ledger.close()
    command(profile, "retry")
    assert command(profile, "list", "--user", "operator", "--agent", "agent")["count"]["total"] == 2
    command(profile, "search", "--query", "restart durable write")


def test_missing_config_and_injected_secrets_are_host_neutral(tmp_path):
    """Missing config uses independent defaults and injected secrets avoid Hermes."""
    home = tmp_path / "absent"
    cfg = load_standalone_config(home, secret_reader=lambda name: None)
    assert cfg["scope"] == {"user_id": "local-user", "agent_id": None}
    assert not home.exists()
    cfg = load_standalone_config(
        home,
        {"qdrant": {"mode": "cloud"}},
        secret_reader=lambda name: {
            "QDRANT_URL": "https://example.com",
            "QDRANT_API_KEY": "fixture",
        }.get(name),
    )
    assert cfg["qdrant"]["api_key"] == "fixture"
    assert not home.exists()


@pytest.mark.parametrize("status", ["PENDING", "FAILED"])
def test_raw_host_events_block_repeated_startup_without_mutation(profile, status):
    """Standalone cannot fail or otherwise mutate raw events without a host extractor."""
    home, _ = profile
    command(profile, "init")
    cfg = load_standalone_config(home)
    ledger = Ledger(home, ledger_namespace(cfg))
    key = ledger.enqueue_event(
        {
            "session_id": "trusted-host",
            "scope": cfg["scope"],
            "user": "private raw host turn",
            "assistant": "raw answer",
        }
    )
    if status == "FAILED":
        ledger.finish("events", key, status)
    before = ledger.row("events", key)
    ledger.close()
    for _ in range(2):
        result = command(profile, "add", "--text", "must remain blocked", ok=False)
        assert result["error"]["type"] == "HostExtractionRequiredError"
        ledger = Ledger(home, ledger_namespace(cfg))
        try:
            assert ledger.row("events", key) == before
            assert not list(ledger.iter_rows("operations"))
        finally:
            ledger.close()
