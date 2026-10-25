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


class RemoteStatusError(RuntimeError):
    """A lifecycle response carried a status code and no usable error body."""

    def __init__(self, status):
        """Keep the status code only, never the remote body."""
        super().__init__("Owner lifecycle request was rejected")
        self.status_code = status


def _control_error(body):
    """Retain a small machine vocabulary, never remote message/body fields."""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None
    code = error.get("type")
    if code not in (
        "KeyError",
        "ValueError",
        "PermissionError",
        "SharedRecoveryRequiredError",
        "TypeError",
    ):
        code = "OwnerEventError"
    return {"error": {"type": code, "retryable": error.get("retryable") is True}}


async def _event_request(url, token, event, *, ca_file, path):
    """Use the same verified, non-redirecting transport for event admission/control."""
    validate_endpoint(url)
    if not isinstance(token, str) or not 32 <= len(token) <= 1000 or not token.isascii():
        raise ValueError("A provisioned service credential is required")
    import httpx

    context = ssl.create_default_context(cafile=ca_file)
    async with httpx.AsyncClient(
        verify=context,
        trust_env=False,
        follow_redirects=False,
        timeout=30,
    ) as client:
        response = await client.post(
            url.removesuffix("/mcp") + path,
            headers={"Authorization": "Bearer " + token},
            json=event,
        )
        if response.status_code != 200:
            if path == "/events/manage" and 400 <= response.status_code < 500:
                try:
                    body = response.json()
                except (ValueError, RecursionError):
                    body = None
                error = _control_error(body)
                if error is not None:
                    return error
            raise RemoteStatusError(response.status_code)
        return response.json()


async def send_event(url, token, event, *, ca_file=None):
    """Acknowledge only a committed write or an explicitly cancelled raw event."""
    result = await _event_request(url, token, event, ca_file=ca_file, path="/events")
    if not isinstance(result, dict) or result.get("status") not in (
        "COMMITTED",
        "CANCELLED",
        "QUARANTINED",
    ):
        raise RuntimeError("Owner lifecycle is not committed; retain the event for retry")
    return result


async def control_event(url, token, event_id, action, *, ca_file=None):
    """Inspect/cancel a device's own event without widening its memory authority."""
    if (
        action not in ("status", "cancel")
        or not isinstance(event_id, str)
        or not 0 < len(event_id) <= 128
    ):
        raise ValueError("Invalid lifecycle control")
    result = await _event_request(
        url, token, {"action": action, "event_id": event_id}, ca_file=ca_file, path="/events/manage"
    )
    error = _control_error(result)
    if error is not None:
        return error
    if not isinstance(result, dict) or not isinstance(result.get("status"), str):
        raise ValueError("Invalid lifecycle control response")
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


HINTS = {
    "tls_verification_failed": "Trust the Owner CA file with --ca-file; never disable verification.",
    "tls_failed": "Check the Owner certificate chain and the TLS listener.",
    "authentication_rejected": "Rotate or reissue the device credential; confirm the Owner policy.",
    "unreachable": "Confirm the Owner address, listener and network path.",
    "timeout": "Confirm the Owner is responsive, then retry within the durable request contract.",
    "protocol_error": "Confirm the endpoint ends in /mcp and serves the expected MCP version.",
    "business_error": "Read the Owner error type; the request reached the Owner.",
}


def remote_error(exc):
    """Return a stable transport diagnostic without remote text or credentials."""
    leaf = exc
    while isinstance(leaf, BaseExceptionGroup) and leaf.exceptions:
        leaf = leaf.exceptions[0]
    reason = "remote_error"
    status = getattr(leaf, "status_code", None) or getattr(
        getattr(leaf, "response", None), "status_code", None
    )
    try:
        import ssl

        import httpx

        modules = [httpx]
        try:
            alternate = http_module()
        except ImportError:  # the optional MCP transport is not installed
            alternate = None
        if alternate is not None and alternate is not httpx:
            modules.append(alternate)
        chain, node = [], leaf
        while node is not None and node not in chain and len(chain) < 8:
            chain.append(node)
            node = node.__cause__ or node.__context__
        if any(isinstance(item, ssl.SSLCertVerificationError) for item in chain):
            reason = "tls_verification_failed"
        elif any(isinstance(item, ssl.SSLError) for item in chain):
            reason = "tls_failed"
        elif isinstance(leaf, tuple(m.TimeoutException for m in modules)) or isinstance(
            leaf, TimeoutError
        ):
            reason = "timeout"
        elif isinstance(
            leaf,
            tuple(m.ConnectError for m in modules)
            + tuple(m.NetworkError for m in modules)
            + (ConnectionError,),
        ):
            reason = "unreachable"
        elif status in (401, 403):
            reason = "authentication_rejected"
        elif status is not None:
            reason = "business_error"
        elif type(leaf).__name__ in {"McpError", "BrokenResourceError"}:
            reason = "protocol_error"
    except ImportError:  # pragma: no cover - httpx ships with the remote extra
        pass
    return {
        "type": type(leaf).__name__,
        "reason": reason,
        "retryable": reason in {"timeout", "unreachable"},
        "hint": HINTS.get(reason, "Inspect the Owner log with the correlation identifier."),
    }


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
