"""Thin local service client and stdio MCP bridge; never opens storage or embeds."""

import json
import os
import re

import httpx

from .shared_protocol import validate_loopback_url
from .shared_service import shared_schemas

MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _collect_bounded(response):
    """Read a hard-capped body without decoding, so compression cannot expand it."""
    body = bytearray()
    # iter_raw() never decodes transfer encodings; combined with the identity request
    # header the cap always measures the real byte count.
    for chunk in response.iter_raw():
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("Shared service response too large")
    return bytes(body)


def call(url, token, name, arguments):
    """Call only a literal loopback address with no redirects or environment proxies."""
    # Convert at the trust boundary: only a bounded numeric port can vary;
    # neither an untrusted URL string nor its host/path reaches HTTPX.
    port = validate_loopback_url(url)
    endpoint = httpx.URL("http://127.0.0.1/v1/tool", port=port)
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("A high-entropy shared credential is required")
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=120) as client:
        with client.stream(
            "POST",
            endpoint,
            headers={
                "Authorization": "Bearer " + token,
                # No compression on the loopback boundary: the size cap must see real bytes.
                "Accept-Encoding": "identity",
            },
            json={"name": name, "arguments": arguments},
        ) as response:
            raw = _collect_bounded(response)
            try:
                result = json.loads(raw)
            except ValueError:
                raise ValueError("Shared service response is not valid JSON") from None
            if not isinstance(result, dict):
                raise ValueError("Shared service response is not a JSON object")
            if response.status_code != 200:
                envelope = result.get("error")
                envelope = envelope if isinstance(envelope, dict) else {}
                code = envelope.get("type", "SharedClientError")
                code = (
                    code
                    if isinstance(code, str) and re.fullmatch(r"[A-Za-z]+Error", code)
                    else "SharedClientError"
                )
                # Preserve machine-readable conflicts without reflecting remote error messages.
                return {
                    "error": {
                        "type": code,
                        "retryable": envelope.get("retryable") is True,
                    }
                }
            return result


def serve_mcp(url, token_env):
    """Expose official stdio MCP while delegating authority to the local owner."""
    import anyio
    from mcp.server.stdio import stdio_server

    from .mcp_server import create_server

    token = os.environ.get(token_env, "").strip()
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
