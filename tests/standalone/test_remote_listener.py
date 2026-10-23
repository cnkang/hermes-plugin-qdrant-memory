"""The HTTPS Owner listener must rebind promptly after a normal restart."""

import os
import socket
from pathlib import Path

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


def test_listener_reuse_policy_and_posix_rebind():
    """POSIX rebinds a closing port; Windows deliberately keeps the option off."""
    first = listener_socket(0)
    enabled = first.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) != 0
    assert enabled is (os.name != "nt")
    port = _leave_time_wait(first)
    if os.name == "nt":
        return
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
