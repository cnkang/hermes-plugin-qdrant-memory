"""Admission control must refuse overflow without parking the accept loop."""

import json
import socket
import threading
from contextlib import closing

from qdrant_memory.shared_service import make_server


class StubOwner:
    """Minimal authenticated owner used to exercise HTTP admission only."""

    def authenticate(self, token):
        """Accept the synthetic bearer used by the test."""
        return {"principal": "stub"}

    def call(self, token, name, arguments):
        """Return a fixed empty search result."""
        return {"memories": []}


def request(port, payload):
    """Send one bounded tool request and return the raw response."""
    body = json.dumps(payload).encode()
    head = (
        f"POST /v1/tool HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        "Authorization: Bearer " + "x" * 40 + "\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode()
    with closing(socket.create_connection(("127.0.0.1", port), timeout=5)) as client:
        client.sendall(head + body)
        chunks = []
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def test_admission_overflow_is_refused_instead_of_parking_the_listener():
    """An exhausted pool answers 429 and keeps the accept loop alive."""
    server = make_server(StubOwner(), 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for _ in range(32):
            server.slots.acquire()
        refused = request(
            server.server_port, {"name": "qdrant_memory_search", "arguments": {"query": "x"}}
        )
        assert refused.startswith(b"HTTP/1.0 429")
        for _ in range(32):
            server.slots.release()
        accepted = request(
            server.server_port, {"name": "qdrant_memory_search", "arguments": {"query": "x"}}
        )
        assert accepted.startswith(b"HTTP/1.0 200")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
