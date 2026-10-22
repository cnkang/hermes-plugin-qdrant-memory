"""The HTTPS MCP listener must not cap admission by accepted connections."""


def test_remote_listener_has_no_pre_request_connection_cap():
    """Idle local sockets must not turn into a global 503 before authentication."""
    from qdrant_memory.remote_gateway import LISTENER_OPTIONS

    assert "limit_concurrency" not in LISTENER_OPTIONS
