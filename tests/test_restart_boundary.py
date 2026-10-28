"""A real listener cut must produce a replayable class, not a final answer."""

import asyncio
import socket
import subprocess
import sys
import threading
import time

import httpx

from qdrant_memory.remote_client import remote_error

SERVER = '''
import sys
import time
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

MARKER = Path(sys.argv[1])
PORT = int(sys.argv[2])


async def slow(request):
    """Signal that the request reached the Owner, then hold it open."""
    MARKER.write_text("reached")
    time.sleep(30)
    return JSONResponse({"ok": True})


app = Starlette(routes=[Route("/mcp", slow, methods=["POST"])])
uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="critical")
'''


def _free_port():
    """Reserve one loopback port and release it for the child process."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_for(condition, timeout=20.0):
    """Wait for one condition without a fixed sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_a_killed_listener_reports_a_replayable_outcome(tmp_path):
    """A process cut during a request must stay retryable with the same ID."""
    script = tmp_path / "owner.py"
    script.write_text(SERVER)
    marker = tmp_path / "reached"
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, str(script), str(marker), str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    outcome = {}

    def listening():
        """Report whether the child accepts a connection yet."""
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            return False

    async def cut():
        """Send one request and return the transport failure it raises."""
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            try:
                await client.post(f"http://127.0.0.1:{port}/mcp", json={"event_id": "turn-1"})
            except Exception as exc:  # the failure class is the assertion target
                return exc
        return None

    def client():
        """Run the synthetic client until the process cut ends its request."""
        outcome["failure"] = asyncio.run(cut())

    thread = threading.Thread(target=client, daemon=True)
    try:
        assert _wait_for(listening), "the synthetic Owner listener did not start"
        thread.start()
        assert _wait_for(marker.exists), "the request never reached the synthetic Owner"
        process.kill()
        thread.join(30)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=20)

    assert not thread.is_alive(), "the client did not observe the process cut"
    failure = outcome.get("failure")
    assert failure is not None, "the client saw a complete response across a process cut"
    diagnostic = remote_error(failure, replay="event_id")
    assert diagnostic["retryable"] is True
    assert diagnostic["reason"] in {"outcome_unknown", "unreachable", "timeout"}
    assert diagnostic["replay"] == "event_id"
