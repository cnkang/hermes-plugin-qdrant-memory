"""HTTPS MCP-to-MCP adapter for provisioned, independent service credentials."""

import json
import os
import ssl
from importlib.metadata import version
from urllib.parse import urlsplit

from .mcp_server import create_tool_server
from .shared_service import shared_schemas
from .tools import validate_arguments


def validate_endpoint(url):
    """Require one explicit HTTPS MCP endpoint with no credential-bearing URL parts."""
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.path == "/mcp"
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and (parsed.port is None or 0 < parsed.port <= 65535)
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("Remote endpoint must be an HTTPS /mcp URL without credentials")
    return url


def http_module():
    """Match the official SDK's HTTP client type without changing the Runtime client."""
    if int(version("mcp").split(".")[0]) >= 2:
        import httpx2

        return httpx2
    import httpx

    return httpx


async def send_event(url, token, event, *, ca_file=None):
    """Submit only hook-authorized content to the opt-in Owner lifecycle extension."""
    validate_endpoint(url)
    if not isinstance(token, str) or not 32 <= len(token) <= 1000 or not token.isascii():
        raise ValueError("A provisioned service credential is required")
    import httpx

    async with httpx.AsyncClient(
        verify=ssl.create_default_context(cafile=ca_file),
        trust_env=False,
        follow_redirects=False,
        timeout=30,
    ) as client:
        response = await client.post(
            url.removesuffix("/mcp") + "/events",
            headers={"Authorization": "Bearer " + token},
            json=event,
        )
        if response.status_code != 200:
            raise RuntimeError("Owner lifecycle admission failed")
        result = response.json()
        if not isinstance(result, dict) or result.get("status") != "COMMITTED":
            raise RuntimeError("Owner lifecycle is not committed; retain the event for retry")
        return result


async def call_remote(url, token, name, arguments, *, ca_file=None):
    """Use official protocol negotiation and preserve Owner receipt/error semantics."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    validate_endpoint(url)
    validate_arguments(name, arguments, shared_schemas())
    if not isinstance(token, str) or not 32 <= len(token) <= 1000 or not token.isascii():
        raise ValueError("A provisioned service credential is required")
    http = http_module()
    context = ssl.create_default_context(cafile=ca_file)

    async def reject_redirect(response):
        if 300 <= response.status_code < 400:
            raise PermissionError("Remote MCP redirects are forbidden")

    async with http.AsyncClient(
        verify=context,
        trust_env=False,
        follow_redirects=False,
        timeout=30,
        headers={"Authorization": "Bearer " + token},
        event_hooks={"response": [reject_redirect]},
    ) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
                if not result.content or not hasattr(result.content[0], "text"):
                    raise ValueError("Invalid remote MCP result")
                value = json.loads(result.content[0].text)
                if not isinstance(value, dict):
                    raise ValueError("Invalid remote MCP result")
                return value


def serve_mcp(url, token_env, *, ca_file=None):
    """Bridge local stdio MCP to HTTPS MCP without storage or database credentials."""
    import anyio
    from mcp.server.stdio import stdio_server

    validate_endpoint(url)
    token = os.environ.get(token_env, "").strip()
    if not 32 <= len(token) <= 1000:
        raise ValueError("Remote service credential environment variable is missing")

    def invoke(name, arguments):
        async def request():
            return await call_remote(url, token, name, arguments, ca_file=ca_file)

        return anyio.run(request)

    server = create_tool_server(shared_schemas, invoke)

    async def run():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(run)
