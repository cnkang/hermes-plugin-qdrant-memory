"""Rejected local requests must preserve an HTTP response without unbounded reads."""

import io
from email.message import Message
from types import SimpleNamespace

import pytest

from qdrant_memory.shared_service import make_server


@pytest.mark.parametrize(
    "rejection", ["missing-token", "invalid-token", "origin", "oversize", "invalid-json"]
)
def test_rejected_body_is_consumed_once_within_the_limit(rejection):
    """An unread body must not turn a local denial into a Windows connection reset."""
    body = b"x" * 131073 if rejection == "oversize" else b"not-json"
    headers = Message()
    headers["Content-Type"] = "application/json"
    headers["Content-Length"] = str(len(body))
    if rejection != "missing-token":
        headers["Authorization"] = "Bearer synthetic"
    if rejection == "origin":
        headers["Origin"] = "http://untrusted.invalid"
    calls, statuses, reads = [], [], []

    class Reader(io.BytesIO):
        """Track bounded reads without relying on a platform-specific reset race."""

        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    def authenticate(_token):
        """Keep invalid credentials rejected before any tool admission."""
        if rejection == "invalid-token":
            raise PermissionError("Synthetic denial")

    owner = SimpleNamespace(authenticate=authenticate, call=lambda *args: calls.append(args))
    with make_server(owner, 0) as server:
        headers["Host"] = f"127.0.0.1:{server.server_port}"
        handler = object.__new__(server.RequestHandlerClass)
        handler.server, handler.path, handler.headers = server, "/v1/tool", headers
        handler.rfile, handler.wfile = Reader(body), io.BytesIO()
        handler.send_response = statuses.append
        handler.send_header = lambda *_args: None
        handler.end_headers = lambda: None
        handler.do_POST()
    assert statuses == [403 if rejection in {"missing-token", "invalid-token", "origin"} else 400]
    assert handler.rfile.tell() == len(body)
    assert reads == [len(body)]
    assert not calls


@pytest.mark.parametrize("framing", ["large", "chunked", "duplicate"])
def test_rejected_body_drain_rejects_unsafe_framing(framing):
    """An invalid sender cannot request a large or ambiguously framed drain."""
    headers = Message()
    headers["Content-Type"] = "application/json"
    headers["Content-Length"] = "131074" if framing == "large" else "8"
    if framing == "chunked":
        headers["Transfer-Encoding"] = "chunked"
    if framing == "duplicate":
        headers["Content-Length"] = "8"
    calls, statuses = [], []
    owner = SimpleNamespace(authenticate=lambda *args: calls.append(args))
    with make_server(owner, 0) as server:
        headers["Host"] = f"127.0.0.1:{server.server_port}"
        handler = object.__new__(server.RequestHandlerClass)
        handler.server, handler.path, handler.headers = server, "/v1/tool", headers
        handler.rfile, handler.wfile = io.BytesIO(b"not-json"), io.BytesIO()
        handler.send_response = statuses.append
        handler.send_header = lambda *_args: None
        handler.end_headers = lambda: None
        handler.do_POST()
    assert statuses == [403]
    assert handler.rfile.tell() == 0
    assert not calls
