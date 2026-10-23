"""The HTTPS Owner listener must rebind promptly after a normal restart."""

import socket

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


def test_listener_rebinds_after_an_accepted_connection_closes():
    """A supervisor restart must not fail with address already in use."""
    port = _leave_time_wait(listener_socket(0))
    replacement = listener_socket(port)
    try:
        assert replacement.getsockname()[1] == port
    finally:
        replacement.close()
