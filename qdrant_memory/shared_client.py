"""Thin local service client and stdio MCP bridge; never opens storage or embeds."""

import os
import re
from urllib.parse import urlsplit

import httpx

from .shared_service import shared_schemas


class SharedClientError(RuntimeError):
    """Expose only a sanitized service error code to the MCP adapter."""


def call(url, token, name, arguments):
    """Call only a literal loopback address with no redirects or environment proxies."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not parsed.port
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Shared service URL must be literal IPv4 loopback with port")
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("A high-entropy shared credential is required")
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=120) as client:
        response = client.post(
            url.rstrip("/") + "/v1/tool",
            headers={"Authorization": "Bearer " + token},
            json={"name": name, "arguments": arguments},
        )
        result = response.json()
        if response.status_code != 200:
            code = result.get("error", {}).get("type", "SharedClientError")
            code = (
                code
                if isinstance(code, str) and re.fullmatch(r"[A-Za-z]+Error", code)
                else "SharedClientError"
            )
            # Preserve machine-readable conflicts without reflecting remote error messages.
            return {
                "error": {
                    "type": code,
                    "retryable": result.get("error", {}).get("retryable") is True,
                }
            }
        return result


def serve_mcp(url, token_env):
    """Expose official stdio MCP while delegating authority to the local owner."""
    import anyio
    from mcp.server.stdio import stdio_server

    from .mcp_server import create_server

    token = os.environ.get(token_env, "")
    if len(token) < 32:
        raise ValueError("Shared credential environment variable is missing")
    server = create_server(
        None,
        tool_schemas=shared_schemas,
        invoke_tool=lambda name, arguments: call(url, token, name, arguments),
    )

    async def run():
        """Keep protocol stdout isolated from the service API."""
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(run)
