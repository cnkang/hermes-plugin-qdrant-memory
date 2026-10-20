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


@pytest.mark.skipif(os.name != "posix", reason="POSIX SIGTERM process semantics")
def test_real_gateway_sigterm_closes_owner_resources(tmp_path):
    """Run actual uvicorn and loopback listeners, with an isolated synthetic Runtime."""
    program = """
import sys
from contextlib import contextmanager
from pathlib import Path
from tests.helpers import runtime
from qdrant_memory import remote_gateway, standalone
root = Path(sys.argv[1])
@contextmanager
def owned(*args, **kwargs):
    rt = runtime(root)
    try:
        yield rt
    finally:
        rt.ledger.close()
        rt.store.close()
        (root / 'closed').write_text('ledger and store closed')
standalone.open_runtime = owned
import qdrant_memory.shared_service as shared
shared.load_policy_document = lambda path: {'destination': {'fixture': True}, 'clients': []}
remote_gateway.serve(root, root / 'policy', port=0, local_port=0, allowed_hosts=['memory.test'])
"""
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    lines = queue.Queue()
    reader = threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        bound = json.loads(lines.get(timeout=15))
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
        assert process.wait(timeout=15) == 0
        assert (tmp_path / "closed").read_text() == "ledger and store closed"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        reader.join(timeout=2)
