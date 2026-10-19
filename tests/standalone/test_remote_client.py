"""The stdio-to-remote adapter uses HTTPS MCP, never the private JSON API."""

import pytest

from qdrant_memory.remote_client import validate_endpoint


@pytest.mark.parametrize(
    "url",
    [
        "http://memory.test/mcp",
        "https://u:p@memory.test/mcp",
        "https://memory.test/mcp?token=x",
        "https://memory.test/mcp#x",
        "https://memory.test/v1/tool",
        "https://memory.test/mcp/",
    ],
)
def test_remote_rejects_unsafe_endpoints(url):
    """Credentials never reach plaintext, embedded credentials or a private API."""
    with pytest.raises(ValueError):
        validate_endpoint(url)


def test_explicit_https_mcp_endpoint():
    """Native and stdio-only clients select the same public transport."""
    assert validate_endpoint("https://memory.example.com/mcp") == "https://memory.example.com/mcp"
