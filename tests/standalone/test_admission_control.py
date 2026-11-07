"""Admission control must refuse overflow without parking the accept loop."""

import json
import socket
import threading
import time
from contextlib import closing

import pytest

from qdrant_memory import shared_service
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
        assert b'"type":"SharedAdmissionError"' in refused
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


def test_partial_request_deadline_releases_every_admission_permit(monkeypatch):
    """A trickled partial request cannot hold a permit past the absolute deadline."""
    monkeypatch.setattr(shared_service, "REQUEST_DEADLINE_SECONDS", 0.4)
    server = make_server(StubOwner(), 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    peers = []
    try:
        for _ in range(32):
            peer = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
            peer.sendall(
                f"POST /v1/tool HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\nX-Pad: ".encode()
            )
            peers.append(peer)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            permits = [server.slots.acquire(blocking=False) for _ in range(32)]
            free = sum(permits)
            for acquired in permits:
                if acquired:
                    server.slots.release()
            if free == 32:
                break
            time.sleep(0.05)
        else:
            pytest.fail("absolute request deadline did not release the admission pool")
        accepted = request(
            server.server_port, {"name": "qdrant_memory_search", "arguments": {"query": "x"}}
        )
        assert accepted.startswith(b"HTTP/1.0 200")
        for peer in peers:
            peer.settimeout(2)
            assert peer.recv(1) == b""
    finally:
        for peer in peers:
            peer.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_deadline_does_not_cut_an_admitted_backend_call(monkeypatch):
    """The read deadline is cancelled once the body is complete."""

    class SlowOwner(StubOwner):
        """Delay the backend beyond the read deadline."""

        def call(self, token, name, arguments):
            time.sleep(0.6)
            return {"memories": []}

    monkeypatch.setattr(shared_service, "REQUEST_DEADLINE_SECONDS", 0.2)
    server = make_server(SlowOwner(), 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = request(
            server.server_port, {"name": "qdrant_memory_search", "arguments": {"query": "x"}}
        )
        assert response.startswith(b"HTTP/1.0 200")
        assert b'"memories"' in response
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_incomplete_declared_body_never_reaches_the_owner(monkeypatch):
    """A valid JSON prefix under a larger Content-Length must not execute."""
    calls = []

    class RecordingOwner(StubOwner):
        """Record whether the backend was invoked."""

        def call(self, token, name, arguments):
            calls.append((name, arguments))
            return {"memories": []}

    monkeypatch.setattr(shared_service, "REQUEST_DEADLINE_SECONDS", 0.3)
    server = make_server(RecordingOwner(), 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    peer = None
    try:
        body = json.dumps({"name": "qdrant_memory_search", "arguments": {"query": "x"}}).encode()
        peer = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        header = (
            f"POST /v1/tool HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n"
            "Authorization: Bearer " + "x" * 40 + "\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body) + 16}\r\nConnection: close\r\n\r\n"
        ).encode()
        peer.sendall(header + body)
        peer.shutdown(socket.SHUT_WR)
        response = b""
        while True:
            chunk = peer.recv(4096)
            if not chunk:
                break
            response += chunk
        assert b" 400 " in response.split(b"\r\n", 1)[0]
        assert calls == []
    finally:
        if peer is not None:
            peer.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_listener_reuse_option_is_exclusive_on_windows(monkeypatch):
    """Windows must not inherit SO_REUSEADDR's live-port sharing."""
    calls = []

    class FakeSocket:
        """Record the socket option the binder applies."""

        def setsockopt(self, level, option, value):
            calls.append((level, option, value))

    monkeypatch.setattr(shared_service.os, "name", "nt")
    monkeypatch.setattr(shared_service.socket, "SO_EXCLUSIVEADDRUSE", 0x4, raising=False)
    shared_service._set_listener_reuse_option(FakeSocket())
    assert calls == [(shared_service.socket.SOL_SOCKET, 0x4, 1)]


def test_windows_listener_fails_closed_without_exclusive_option(monkeypatch):
    """A Windows build without SO_EXCLUSIVEADDRUSE must not fall back to sharing."""

    class FakeSocket:
        """Record the socket option the binder applies."""

        def setsockopt(self, level, option, value):
            raise AssertionError("must not apply a reuse option")

    monkeypatch.setattr(shared_service.os, "name", "nt")
    monkeypatch.delattr(shared_service.socket, "SO_EXCLUSIVEADDRUSE", raising=False)
    with pytest.raises(RuntimeError):
        shared_service._set_listener_reuse_option(FakeSocket())


def test_listener_reuse_option_stays_reusable_on_posix(monkeypatch):
    """POSIX keeps SO_REUSEADDR so a restart can bind after TIME_WAIT."""
    calls = []

    class FakeSocket:
        """Record the socket option the binder applies."""

        def setsockopt(self, level, option, value):
            calls.append((level, option, value))

    monkeypatch.setattr(shared_service.os, "name", "posix")
    shared_service._set_listener_reuse_option(FakeSocket())
    assert calls == [(shared_service.socket.SOL_SOCKET, shared_service.socket.SO_REUSEADDR, 1)]
