"""Shared-protocol constants and the one loopback URL validator.

Kept dependency-free so the service, the client and the reader cannot drift apart and so
importing it never pulls in storage or HTTP libraries.
"""

from __future__ import annotations

from urllib.parse import urlsplit

MARKER = "qdrant-memory-shared-v1"
VISIBILITIES = frozenset({"private", "shared", "project"})

_URL_REJECTED = "Shared service URL must be literal IPv4 loopback with port"


def validate_loopback_url(url: object) -> int:
    """Return the numeric port of a literal loopback URL or raise a redacted error."""
    if not isinstance(url, str):
        raise ValueError(_URL_REJECTED)
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(_URL_REJECTED)
    try:
        raw_port = parsed.port
    except ValueError:
        # Never reflect the offending port text; it may contain untrusted input.
        raise ValueError(_URL_REJECTED) from None
    if raw_port is None:
        raise ValueError(_URL_REJECTED)
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise ValueError("Shared service port must be between 1 and 65535")
    return port
