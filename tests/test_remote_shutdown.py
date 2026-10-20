"""A real non-PID-1 Gateway process must unwind its Owner resources on SIGTERM."""

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]

# The child process must exercise the checkout under test, never an installed
# copy, and must not depend on the ambient sys.path of the test runner. It
# therefore loads the deterministic fixture by absolute path instead of
# importing the ``tests`` package, which other checkouts may shadow.
PROGRAM = """
import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path

root = Path(sys.argv[1])
repo = Path(sys.argv[2])

spec = importlib.util.spec_from_file_location(
    "hermes_behavior_helpers", repo / "tests" / "helpers.py"
)
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)

from qdrant_memory import remote_gateway, standalone


@contextmanager
def owned(*args, **kwargs):
    rt = helpers.runtime(root)
    try:
        yield rt
    finally:
        rt.ledger.close()
        rt.store.close()
        (root / "closed").write_text("ledger and store closed")


standalone.open_runtime = owned
import qdrant_memory.shared_service as shared

shared.load_policy_document = lambda path: {"destination": {"fixture": True}, "clients": []}
remote_gateway.serve(root, root / "policy", port=0, local_port=0, allowed_hosts=["memory.test"])
"""


def _child_env():
    """Expose the checkout under test to the child process, ahead of site-packages."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), *([existing] if existing else [])])
    return env


@pytest.mark.skipif(os.name != "posix", reason="POSIX SIGTERM process semantics")
def test_real_gateway_sigterm_closes_owner_resources(tmp_path):
    """Run actual uvicorn and loopback listeners, with an isolated synthetic Runtime."""
    process = subprocess.Popen(
        [sys.executable, "-c", PROGRAM, str(tmp_path), str(ROOT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(ROOT),
        env=_child_env(),
    )
    lines = queue.Queue()
    errors = []

    def pump_stdout():
        """Move every child stdout line into the queue without blocking the parent."""
        for line in iter(process.stdout.readline, ""):
            lines.put(line)

    def pump_stderr():
        """Keep the child diagnostics available for any failure report."""
        errors.extend(iter(process.stderr.readline, ""))

    pumps = [
        threading.Thread(target=pump_stdout, daemon=True),
        threading.Thread(target=pump_stderr, daemon=True),
    ]
    for pump in pumps:
        pump.start()

    def diagnostics():
        """Describe how the child process ended."""
        return f"exit={process.poll()} stderr={''.join(errors).strip()!r}"

    def read_binding():
        """Wait for the gateway's own readiness line and return its bound ports."""
        deadline = time.monotonic() + 15
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"gateway did not bind in 15s: {diagnostics()}")
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                raise AssertionError(f"gateway did not bind in 15s: {diagnostics()}")
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict) and payload.get("bound"):
                return payload

    try:
        bound = read_binding()
        deadline = time.monotonic() + 15
        with httpx.Client(trust_env=False, timeout=1) as client:
            while True:
                try:
                    response = client.get(
                        f"http://127.0.0.1:{bound['remote_port']}/mcp",
                        headers={"Host": "memory.test"},
                    )
                    assert response.status_code == 403
                    break
                except httpx.TransportError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.02)
        os.kill(process.pid, signal.SIGTERM)
        code = process.wait(timeout=15)
        assert code == 0, f"gateway did not exit cleanly: {diagnostics()}"
        assert (tmp_path / "closed").read_text() == "ledger and store closed"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        # The pipes reach EOF once the child is gone; drain them before closing
        # so no reader thread can fail on an already closed stream.
        for pump in pumps:
            pump.join(timeout=2)
        process.stdout.close()
        process.stderr.close()
