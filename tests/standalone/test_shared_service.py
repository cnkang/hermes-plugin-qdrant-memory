"""Independent clients, real storage and owner-process interruption acceptance."""

import asyncio
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from queue import Queue
from threading import Thread

import httpx
import pytest

from qdrant_memory.models import Scope
from qdrant_memory.shared_service import SharedOwner, load_policy
from qdrant_memory.shared_state import SharedRecoveryRequiredError
from qdrant_memory.standalone import open_runtime

from .test_standalone import command
from .test_standalone import profile as standalone_profile

TOKENS = {
    name: hashlib.sha256(("synthetic-credential-" + name).encode()).hexdigest()
    for name in ("hermes", "dsh", "reader", "private", "foreign")
}


def pytest_generate_tests(metafunc):
    """Collect Server acceptance only when a disposable endpoint was supplied."""
    if "profile" in metafunc.fixturenames:
        modes = ["embedded"]
        if metafunc.config.getoption("--qdrant-test-url"):
            modes.append("server")
        metafunc.parametrize("profile", modes, indirect=True)


@pytest.fixture
def profile(tmp_path, request):
    """Select real embedded or explicitly supplied disposable Server collection."""
    url = request.config.getoption("--qdrant-test-url")
    for value in standalone_profile.__wrapped__(tmp_path):
        home, _ = value
        collection = "shared_test_" + hashlib.sha256(str(home).encode()).hexdigest()[:24]
        if request.param == "server":
            path = home / "qdrant-memory.json"
            cfg = json.loads(path.read_text())
            cfg["qdrant"] = {"mode": "server", "url": url, "collection": collection}
            path.write_text(json.dumps(cfg))
        try:
            yield value
        finally:
            if request.param == "server":
                from qdrant_client import QdrantClient

                with closing(QdrantClient(url=url)) as client:
                    client.delete_collection(collection)


def policy(home):
    """Create fixed explicit grants in a private operator file."""
    clients = []
    for name, token in TOKENS.items():
        clients.append(
            {
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "principal": name,
                "owner_id": "other" if name == "foreign" else "user",
                "space_id": "secret" if name == "private" else "project",
                "permission": "read" if name == "reader" else "write",
                "visibility": "private" if name == "private" else "shared",
            }
        )
    path = home / "shared-policy.json"
    path.write_text(json.dumps({"clients": clients}))
    path.chmod(0o600)
    return path


@contextmanager
def owner_process(profile, crash=None):
    """Launch a separate owner; injected kill points only exist in the test process."""
    home, env = profile
    path = policy(home)
    prefix = "import os, signal\nif os.name == 'nt': signal.signal(signal.SIGBREAK, signal.default_int_handler)\n"
    if crash == "prepared":
        prefix += "from qdrant_memory.runtime import Runtime\nRuntime.commit = lambda *a, **k: __import__('os')._exit(71)\n"
    elif crash == "ack":
        prefix += "from qdrant_memory.qdrant_store import QdrantStore\noriginal = QdrantStore.upsert\ndef killed(self, records):\n original(self, records)\n __import__('os')._exit(72)\nQdrantStore.upsert = killed\n"
    elif crash == "plan":
        prefix += "from qdrant_memory.shared_service import SharedOwner\nSharedOwner._apply = lambda *a, **k: __import__('os')._exit(73)\n"
    script = (
        prefix
        + f"from qdrant_memory.shared_service import serve\nserve({str(home)!r}, {str(path)!r}, 0)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        env=env,
        cwd=home,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    ready = Queue()
    reader = Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        line = ready.get(timeout=30)
        assert line, process.stderr.read()
        url = json.loads(line)["url"]
        yield url, process
    finally:
        if process.poll() is None:
            process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        reader.join(timeout=5)
        process.stdout.close()
        process.stderr.close()


def client(profile, url, identity, tool, **arguments):
    """Use an independent CLI process per call, with a credential in its environment."""
    home, env = profile
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "qdrant_memory.standalone",
            "shared-call",
            "--url",
            url,
            "--tool",
            "qdrant_memory_" + tool,
            "--arguments",
            json.dumps(arguments),
        ],
        env={**env, "QDRANT_MEMORY_SHARED_TOKEN": TOKENS[identity]},
        cwd=home,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout, result.stderr
    return json.loads(result.stdout)


def test_independent_clients_isolation_versions_and_restart(profile):
    """A saves, B reads/updates; private/foreign/read-only grants fail closed."""
    command(profile, "init")
    legacy = command(profile, "add", "--text", "legacy private")
    with owner_process(profile) as (url, _):
        added = client(profile, url, "hermes", "add", text="Project uses SQLite", request_id="a1")
        assert added.get("action") == "ADD", added
        hits = client(profile, url, "dsh", "search", query="Project uses SQLite")["memories"]
        assert [h["id"] for h in hits] == [added["id"]]
        assert hits[0]["created_by"] == "hermes"
        updated = client(
            profile,
            url,
            "dsh",
            "update",
            id=added["id"],
            text="Project uses WAL",
            expected_version=added["version"],
            request_id="b1",
        )
        assert updated["version"] != added["version"]
        assert (
            client(profile, url, "hermes", "search", query="WAL")["memories"][0]["text"]
            == "Project uses WAL"
        )
        stale = client(
            profile,
            url,
            "hermes",
            "update",
            id=added["id"],
            text="stale",
            expected_version=added["version"],
            request_id="stale",
        )
        assert stale["error"]["type"] == "SharedConflictError"
        # Successful response replay is stable even after later edits.
        assert (
            client(profile, url, "hermes", "add", text="Project uses SQLite", request_id="a1")
            == added
        )
        assert (
            client(profile, url, "hermes", "add", text="different", request_id="a1")["error"][
                "type"
            ]
            == "SharedConflictError"
        )
        for identity in ("foreign", "private"):
            assert not client(profile, url, identity, "search", query="WAL")["memories"]
            for tool in ("update", "delete"):
                args = {"text": "bad"} if tool == "update" else {}
                assert (
                    client(
                        profile,
                        url,
                        identity,
                        tool,
                        id=added["id"],
                        request_id=tool,
                        expected_version=updated["version"],
                        **args,
                    )["error"]["type"]
                    == "PermissionError"
                )
        for tool in ("add", "update", "delete"):
            args = {"request_id": tool}
            if tool != "delete":
                args["text"] = "denied"
            if tool != "add":
                args.update(id=added["id"], expected_version=updated["version"])
            assert (
                client(profile, url, "reader", tool, **args)["error"]["type"] == "PermissionError"
            )
        assert client(profile, url, "reader", "search", query="WAL")["memories"]
        assert (
            client(profile, url, "dsh", "search", query="x", owner_id="user")["error"]["type"]
            == "ValueError"
        )
        assert (
            client(
                profile,
                url,
                "dsh",
                "delete",
                id=legacy["id"],
                expected_version="x",
                request_id="legacy",
            )["error"]["type"]
            == "PermissionError"
        )
        private = client(profile, url, "private", "add", text="private secret", request_id="p1")
        assert private["action"] == "ADD"
        assert len(client(profile, url, "dsh", "search", query="private secret")["memories"]) == 1
        # Another direct writer cannot acquire the owner's profile lease.
        assert (
            command(profile, "add", "--text", "busy", ok=False)["error"]["type"]
            == "WriterBusyError"
        )
    with owner_process(profile) as (url, _):
        assert (
            client(profile, url, "hermes", "search", query="WAL")["memories"][0]["version"]
            == updated["version"]
        )
        assert (
            client(
                profile,
                url,
                "dsh",
                "update",
                id=added["id"],
                text="Project uses WAL",
                expected_version=added["version"],
                request_id="b1",
            )
            == updated
        )
        deleted = client(
            profile,
            url,
            "dsh",
            "delete",
            id=added["id"],
            expected_version=updated["version"],
            request_id="del",
        )
        assert deleted["action"] == "DELETE"
        recreated = client(
            profile, url, "hermes", "add", text="Project uses SQLite", request_id="a2"
        )
        assert recreated["id"] == added["id"]
        assert recreated["version"] != added["version"]
        assert (
            client(
                profile,
                url,
                "dsh",
                "delete",
                id=recreated["id"],
                expected_version=added["version"],
                request_id="aba",
            )["error"]["type"]
            == "SharedConflictError"
        )


@pytest.mark.parametrize("stage", ["plan", "prepared", "ack"])
def test_killed_owner_replays_immutable_plan_without_duplicates(profile, stage):
    """SIGKILL-equivalent exit before/after Qdrant writes leaves one canonical record."""
    command(profile, "init")
    with owner_process(profile, stage) as (url, process):
        failed = client(
            profile, url, "hermes", "add", text="durable crash fact", request_id="crash-1"
        )
        assert "error" in failed
        process.wait(timeout=10)
        assert process.returncode in {71, 72, 73}
    # A plan-only crash must not be overtaken by legacy reset/delete/tools.
    assert (
        command(profile, "delete-all", "--user", "user", "--all-agents", "--confirm", ok=False)[
            "error"
        ]["type"]
        == "SharedRecoveryRequiredError"
    )
    assert (
        command(profile, "add", "--text", "cannot overtake plan", ok=False)["error"]["type"]
        == "SharedRecoveryRequiredError"
    )
    with owner_process(profile) as (url, _):
        replay = client(
            profile, url, "hermes", "add", text="durable crash fact", request_id="crash-1"
        )
        assert replay["action"] == "ADD"
        hits = client(profile, url, "dsh", "search", query="durable crash fact")["memories"]
        assert len(hits) == 1
        assert hits[0]["id"] == replay["id"]
        assert hits[0]["version"] == replay["version"]
    command(profile, "delete-all", "--user", "user", "--all-agents", "--confirm")
    with owner_process(profile) as (url, _):
        assert not client(profile, url, "dsh", "search", query="durable crash fact")["memories"]


def test_concurrent_processes_and_transport_boundaries(profile):
    """Concurrent inserts/readers serialize; stale contenders produce exactly one winner."""
    command(profile, "init")
    with owner_process(profile) as (url, _):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(
                    lambda i: client(
                        profile, url, "hermes", "add", text="concurrent fact", request_id=f"add-{i}"
                    ),
                    range(4),
                )
            )
            assert len({r["id"] for r in results}) == 1
            reads = list(
                pool.map(lambda _: client(profile, url, "reader", "search", query="fact"), range(4))
            )
            assert all(len(r["memories"]) == 1 for r in reads)
            revision = reads[0]["memories"][0]["version"]
            updates = list(
                pool.map(
                    lambda i: client(
                        profile,
                        url,
                        "dsh",
                        "update",
                        id=results[0]["id"],
                        text=f"winner {i}",
                        request_id=f"update-{i}",
                        expected_version=revision,
                    ),
                    range(4),
                )
            )
            assert sum("error" not in r for r in updates) == 1
            assert all(
                r.get("error", {}).get("type") == "SharedConflictError"
                for r in updates
                if "error" in r
            )
        with httpx.Client(trust_env=False) as http:
            body = {"name": "qdrant_memory_search", "arguments": {"query": "fact"}}
            headers = {"Authorization": "Bearer " + TOKENS["reader"]}
            assert http.post(url + "/v1/tool", json=body).status_code == 403
            assert (
                http.post(
                    url + "/v1/tool", json=body, headers={"Authorization": "Bearer unknown"}
                ).status_code
                == 403
            )
            assert (
                http.post(
                    url + "/v1/tool", json=body, headers={**headers, "Origin": "http://evil.test"}
                ).status_code
                == 403
            )
            assert (
                http.post(
                    url + "/v1/tool", json=body, headers={**headers, "Host": "evil.test"}
                ).status_code
                == 403
            )
            assert (
                http.post(url + "/v1/tool", content="x" * 131073, headers=headers).status_code
                == 403
            )


def test_disconnected_write_and_real_mcp_bridge(profile):
    """Disconnect never cancels durable work; official MCP sessions share the store."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    command(profile, "init")
    home, env = profile
    with owner_process(profile) as (url, _):
        port = int(url.rsplit(":", 1)[1])
        body = json.dumps(
            {
                "name": "qdrant_memory_add",
                "arguments": {"text": "disconnect fact", "request_id": "disconnect"},
            }
        ).encode()
        with socket.create_connection(("127.0.0.1", port)) as connection:
            connection.sendall(
                (
                    f"POST /v1/tool HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {TOKENS['hermes']}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
                ).encode()
                + body
            )
        replay = client(
            profile, url, "hermes", "add", text="disconnect fact", request_id="disconnect"
        )
        assert replay["action"] == "ADD"

        async def check():
            """Run independent official SDK stdio bridges for Hermes fallback and DSH."""

            def parameters(identity):
                return StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "qdrant_memory.standalone", "shared-mcp", "--url", url],
                    env={**env, "QDRANT_MEMORY_SHARED_TOKEN": TOKENS[identity]},
                )

            async with stdio_client(parameters("hermes")) as (read_a, write_a):
                async with ClientSession(read_a, write_a) as a:
                    await a.initialize()
                    saved = await a.call_tool(
                        "qdrant_memory_add", {"text": "MCP cross agent", "request_id": "mcp1"}
                    )
                    assert not saved.model_dump(by_alias=True)["isError"]
                    identifier = json.loads(saved.content[0].text)["id"]
            # New DSH process/session, with no Hermes conversation context.
            async with stdio_client(parameters("dsh")) as (read_b, write_b):
                async with ClientSession(read_b, write_b) as b:
                    await b.initialize()
                    result = await b.call_tool("qdrant_memory_search", {"query": "MCP cross agent"})
                    assert not result.model_dump(by_alias=True)["isError"]
                    assert identifier in {
                        h["id"] for h in json.loads(result.content[0].text)["memories"]
                    }
                    forged = await b.call_tool(
                        "qdrant_memory_search", {"query": "x", "agent_id": "hermes"}
                    )
                    assert forged.model_dump(by_alias=True)["isError"]

        asyncio.run(check())


def test_legacy_matching_scope_stays_private_and_poisoned_writer_blocks(profile):
    """Even reserved-scope legacy payloads need an explicit marker; failures stop writes."""
    command(profile, "init")
    home, _ = profile
    grants = load_policy(policy(home))
    with open_runtime(home) as runtime:
        legacy = runtime.add("legacy reserved scope", Scope("user", "shared:v1:project"))
        owner = SharedOwner(runtime, grants)
        assert not owner.call(TOKENS["dsh"], "qdrant_memory_search", {"query": "legacy"})[
            "memories"
        ]
        with pytest.raises(PermissionError):
            owner.call(
                TOKENS["dsh"],
                "qdrant_memory_delete",
                {"id": legacy["id"], "request_id": "bad", "expected_version": "x"},
            )
        original = runtime.store.upsert
        runtime.store.upsert = lambda _: (_ for _ in ()).throw(ValueError("SECRET must not escape"))
        with pytest.raises(ValueError):
            owner.call(
                TOKENS["hermes"], "qdrant_memory_add", {"text": "failed", "request_id": "f1"}
            )
        with pytest.raises(SharedRecoveryRequiredError):
            owner.call(TOKENS["dsh"], "qdrant_memory_add", {"text": "blocked", "request_id": "f2"})
        runtime.store.upsert = original


def test_policy_validation(tmp_path):
    """Ambiguous identity grants and publicly readable operator policies are rejected."""
    path = policy(tmp_path)
    if os.name != "nt":
        path.chmod(0o644)
        with pytest.raises(PermissionError):
            load_policy(path)
        path.chmod(0o600)
    config = json.loads(path.read_text())
    config["clients"][1]["visibility"] = "private"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        load_policy(path)


def test_embedding_outage_preserves_failed_plan_for_operator_retry(profile):
    """A terminal dependency failure cannot be bypassed by another writer or restart."""
    home, _ = profile
    path = home / "qdrant-memory.json"
    cfg = json.loads(path.read_text())
    cfg["embedding"]["document_instruction"] = "doc:"
    cfg["write"] = {"max_attempts": 2, "backoff_base_seconds": 0, "backoff_max_seconds": 0}
    path.write_text(json.dumps(cfg))
    command(profile, "init")
    with owner_process(profile) as (url, _):
        result = client(
            profile, url, "hermes", "add", text="restart failed shared write", request_id="outage"
        )
        assert "error" in result
        assert "SECRET" not in json.dumps(result)
        assert (
            client(profile, url, "dsh", "add", text="cannot overtake", request_id="overtake")[
                "error"
            ]["type"]
            == "SharedRecoveryRequiredError"
        )
    # Restart also refuses readiness; a second process cannot silently bypass FAILED work.
    assert (
        command(
            profile,
            "shared-serve",
            "--policy",
            str(home / "shared-policy.json"),
            "--port",
            "0",
            ok=False,
        )["error"]["type"]
        == "SharedRecoveryRequiredError"
    )
    # Existing operator retry workflow repairs FAILED Runtime work while no owner runs.
    assert "error" not in command(profile, "retry")
    with owner_process(profile) as (url, _):
        result = client(
            profile, url, "hermes", "add", text="restart failed shared write", request_id="outage"
        )
        assert result["action"] == "ADD"
        hits = client(profile, url, "dsh", "search", query="failed shared write")["memories"]
        assert len(hits) == 1
        assert hits[0]["id"] == result["id"]


def test_error_and_audit_output_contains_no_secrets(profile, caplog):
    """Transport errors and audit decisions never echo tokens, memory or dependency text."""
    import logging
    import threading

    from qdrant_memory.shared_client import call
    from qdrant_memory.shared_service import make_server

    command(profile, "init")
    home, _ = profile
    with open_runtime(home) as runtime:
        owner = SharedOwner(runtime, load_policy(policy(home)))
        original = runtime.store.upsert

        def failed(_):
            """Simulate a dependency whose exception embeds sensitive response text."""
            raise ValueError("SYNTHETIC-SECRET-RESPONSE")

        runtime.store.upsert = failed
        with make_server(owner, 0) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with caplog.at_level(logging.INFO, logger="qdrant_memory.shared_service"):
                    result = call(
                        f"http://127.0.0.1:{server.server_port}",
                        TOKENS["hermes"],
                        "qdrant_memory_add",
                        {"text": "SYNTHETIC-PRIVATE-MEMORY", "request_id": "audit"},
                    )
                rendered = json.dumps(result) + caplog.text
                assert result["error"]["type"] == "ValueError"
                assert "decision=deny" in caplog.text
                assert not any(
                    value in rendered
                    for value in (
                        TOKENS["hermes"],
                        "SYNTHETIC-SECRET-RESPONSE",
                        "SYNTHETIC-PRIVATE-MEMORY",
                    )
                )
            finally:
                server.shutdown()
                thread.join(timeout=10)
                runtime.store.upsert = original
