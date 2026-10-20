"""Drive production Owner service startup, dispatch and failure/drain boundaries."""

import asyncio
import json
import signal
from contextlib import contextmanager

import pytest
import uvicorn

from qdrant_memory.remote_client import call_remote, http_module
from qdrant_memory.remote_gateway import _graceful_sigterm, serve
from qdrant_memory.shared_client import call
from qdrant_memory.shared_service import make_server

from ..helpers import LLM, runtime
from .test_device_grants import grant


@pytest.mark.parametrize("lifecycle", [False, True])
@pytest.mark.parametrize("outcome", ["complete", "startup-failure", "crash"])
def test_service_shares_owner_and_drains_before_runtime_close(
    tmp_path, monkeypatch, lifecycle, outcome
):
    """Only replace the outer uvicorn loop; local HTTP and MCP dispatch stay real."""
    rt = runtime(tmp_path)
    token = "a" * 40
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"clients": [grant(token, "mac")]}))
    policy.chmod(0o600)
    binding = {"synthetic_transport_fixture": True}
    events, local_servers = [], []

    @contextmanager
    def owned(home, **kwargs):
        assert kwargs["shared_owner"] is True
        assert kwargs["destination_policy"] is binding
        if lifecycle:
            assert kwargs["extractor"] is not None
            rt.extractor = kwargs["extractor"]
        try:
            yield rt
        finally:
            assert events[-1] == "local-drained"
            rt.ledger.close()
            rt.store.close()
            events.append("runtime-closed")

    def local(owner, port):
        assert owner.runtime is rt
        server = make_server(owner, port)
        shutdown = server.shutdown

        def drain():
            shutdown()
            events.append("local-drained")

        monkeypatch.setattr(server, "shutdown", drain)
        local_servers.append(server)
        return server

    class Model(LLM):
        def __init__(self, cfg):
            assert cfg is rt.cfg["llm"]

        def close(self):
            events.append("model-closed")

    monkeypatch.setattr("qdrant_memory.standalone.open_runtime", owned)
    monkeypatch.setattr("qdrant_memory.standalone.load_standalone_config", lambda _: rt.cfg)
    monkeypatch.setattr("qdrant_memory.owner_llm.OwnerLLM", Model)
    monkeypatch.setattr("qdrant_memory.shared_service.make_server", local)

    def fixture_admission(path):
        # This transport unit fixture does not exercise POSIX protected-file admission.
        # The real platform fail-closed contract has its own security tests.
        assert path == policy

    monkeypatch.setattr("qdrant_memory.shared_security.private_entry", fixture_admission)
    monkeypatch.setattr(
        "qdrant_memory.shared_service.load_policy_document",
        lambda _: {"destination": binding, "clients": [grant(token, "mac")]},
    )
    monkeypatch.setattr(
        "qdrant_memory.shared_security.validate_destination",
        lambda home, cfg, received: received is binding or pytest.fail("Changed destination"),
    )

    def run(server, *, sockets):
        assert sockets[0].getsockname()[0] == "127.0.0.1"
        assert server.config.proxy_headers is False
        assert server.config.access_log is False
        if outcome == "crash":
            raise OSError("synthetic listener failure")
        server.started = outcome != "startup-failure"
        if not server.started:
            return
        saved = call(
            f"http://127.0.0.1:{local_servers[0].server_port}",
            token,
            "qdrant_memory_add",
            {"text": "cats one Owner", "request_id": "local"},
        )
        assert "error" not in saved
        app = server.config.app
        http = http_module()
        constructor = http.AsyncClient
        monkeypatch.setattr(
            http,
            "AsyncClient",
            lambda **kw: constructor(transport=http.ASGITransport(app=app), **kw),
        )

        async def remote():
            async with app.router.lifespan_context(app):
                result = await call_remote(
                    "https://memory.test/mcp", token, "qdrant_memory_search", {"query": "cats"}
                )
                assert result["memories"][0]["text"] == "cats one Owner"

        asyncio.run(remote())

    monkeypatch.setattr(uvicorn.Server, "run", run)
    arguments = {"port": 0, "local_port": 0, "allowed_hosts": ["memory.test"]}
    if outcome == "complete":
        serve(tmp_path, policy, enable_lifecycle=lifecycle, **arguments)
    else:
        expected = OSError if outcome == "crash" else RuntimeError
        with pytest.raises(expected):
            serve(tmp_path, policy, enable_lifecycle=lifecycle, **arguments)
    assert events == ["local-drained", "runtime-closed"] + (["model-closed"] if lifecycle else [])
    assert local_servers[0].socket.fileno() == -1


def test_termination_handler_restores_the_callers_signal_policy():
    """A graceful exit raises through cleanup and never leaves a changed handler."""
    previous = signal.getsignal(signal.SIGTERM)
    with _graceful_sigterm():
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(SystemExit) as stopped:
            handler(signal.SIGTERM, None)
        assert stopped.value.code == 0
    assert signal.getsignal(signal.SIGTERM) == previous
