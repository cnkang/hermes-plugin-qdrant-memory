"""The HTTPS Owner listener must rebind promptly after a normal restart."""

import os
import socket
from pathlib import Path

import pytest

from qdrant_memory.remote_gateway import listener_socket


def _leave_time_wait(listener):
    """Accept then close a connection so the listening address holds TIME_WAIT."""
    port = listener.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    try:
        peer, _ = listener.accept()
        peer.close()
    finally:
        client.close()
        listener.close()
    return port


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows port reuse semantics differ; protected service is unsupported there",
)
def test_listener_rebinds_after_an_accepted_connection_closes():
    """A supervisor restart must not fail with address already in use."""
    port = _leave_time_wait(listener_socket(0))
    replacement = listener_socket(port)
    try:
        assert replacement.getsockname()[1] == port
    finally:
        replacement.close()


def test_standalone_resolves_a_relative_home(monkeypatch, tmp_path):
    """The standalone entry point normalizes --home before dispatching."""
    from qdrant_memory import standalone

    seen = {}

    def capture(args, home, **kwargs):
        """Record the resolved home and stop before any store access."""
        seen["home"] = home
        return {"ok": True}

    monkeypatch.setattr(standalone.cli, "run", capture)
    monkeypatch.chdir(tmp_path)
    assert standalone.main(["--home", ".", "verify"]) == 0
    assert Path(seen["home"]).resolve() == tmp_path.resolve()
