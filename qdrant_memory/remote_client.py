"""HTTPS MCP-to-MCP adapter for provisioned, independent service credentials."""

import json
import os
import random
import ssl
import time
from importlib.metadata import version
from urllib.parse import urlsplit

from .mcp_server import create_tool_server
from .shared_service import shared_schemas
from .tools import validate_arguments

REQUEST_TIMEOUT_SECONDS = 30

# In-process replay budget. Only a failure that returns inside the fast-failure
# window is replayed here; a caller's durable outbox owns longer outages.
RETRY_ATTEMPTS = 3
RETRY_WINDOW_SECONDS = 5.0
RETRY_BASE_SECONDS = 0.2
RETRY_CAP_SECONDS = 1.0
RETRY_AFTER_MAX_DIGITS = 9

RETRYABLE_REASONS = frozenset(
    {"timeout", "unreachable", "outcome_unknown", "temporarily_unavailable"}
)
RETRYABLE_STATUSES = frozenset({408, 429, 502, 503, 504})


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
    """A lifecycle response carried a rejection status and no usable result."""

    def __init__(self, status, *, error_type=None, retryable=None, retry_after=None):
        """Keep the status code and one machine type, never the remote body."""
        super().__init__("Owner lifecycle request was rejected")
        self.status_code = status
        self.error_type = error_type
        self.retryable = retryable
        self.retry_after = retry_after


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


def _machine_failure(error_type, retryable, retry_after=None):
    """Return one full envelope for a machine-readable Owner rejection."""
    if error_type == "SharedRecoveryRequiredError":
        reason = "recovery_required"
    elif retryable is True:
        reason = "temporarily_unavailable"
    else:
        reason = "business_error"
    result = {
        "type": error_type,
        "reason": reason,
        "retryable": reason in RETRYABLE_REASONS,
        "hint": HINTS[reason],
    }
    if result["retryable"]:
        result["replay"] = "event_id"
        if retry_after is not None:
            result["retry_after"] = retry_after
    return result


def _response_body(response):
    """Read a bounded JSON body only to recover the machine rejection type."""
    try:
        return response.json()
    except (ValueError, RecursionError):
        return None


def _parse_retry_after(value):
    """Convert only a bounded ASCII decimal Retry-After value to seconds."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if (
        not value
        or len(value) > RETRY_AFTER_MAX_DIGITS
        or not value.isascii()
        or not value.isdigit()
    ):
        return None
    return int(value)


async def _event_request(url, token, body, *, ca_file, path):
    """Post one event body over the verified, non-redirecting transport."""
    import httpx

    validate_endpoint(url)
    if not isinstance(token, str) or not 32 <= len(token) <= 1000 or not token.isascii():
        raise ValueError("A provisioned service credential is required")
    context = ssl.create_default_context(cafile=ca_file)
    async with httpx.AsyncClient(
        verify=context,
        trust_env=False,
        follow_redirects=False,
        timeout=REQUEST_TIMEOUT_SECONDS,
    ) as client:
        response = await client.post(
            url.removesuffix("/mcp") + path,
            headers={"Authorization": "Bearer " + token},
            json=body,
        )
        if response.status_code == 200:
            return response.json()
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        # A rejection body carries the reason and the Owner's own retry verdict.
        # It never contains memory text, so only the machine fields are retained.
        error = _control_error(_response_body(response))
        if error is not None:
            raise RemoteStatusError(
                response.status_code,
                error_type=error["error"]["type"],
                retryable=error["error"]["retryable"],
                retry_after=retry_after,
            )
        raise RemoteStatusError(response.status_code, retry_after=retry_after)


async def _retry_sleep(delay):
    """Sleep between replay attempts without holding a transport open."""
    import anyio

    await anyio.sleep(delay)


RETRY_SLEEP = _retry_sleep
RETRY_CLOCK = time.monotonic


async def _replay(send, *, sleep=None, clock=None):
    """Replay one idempotent request while the failure is fast and retryable.

    The caller must pass the same request identity on every attempt. The Owner
    keys tool mutations by ``request_id`` and lifecycle events by ``event_id``,
    so a replayed request converges on the first receipt instead of adding a
    second record. A slow failure is not replayed here: the caller's durable
    outbox owns that delay, and an in-process retry would double the outage.
    """
    sleep = sleep or RETRY_SLEEP
    clock = clock or RETRY_CLOCK
    for attempt in range(RETRY_ATTEMPTS):
        started = clock()
        try:
            return await send()
        except Exception as exc:
            elapsed = clock() - started
            failure = remote_error(exc)
            if (
                not failure["retryable"]
                or attempt + 1 == RETRY_ATTEMPTS
                or elapsed > RETRY_WINDOW_SECONDS
                or (failure.get("retry_after") or 0) > RETRY_CAP_SECONDS
            ):
                raise
            cap = min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * 2**attempt)
            delay = random.uniform(0, cap)
            retry_after = failure.get("retry_after")
            if retry_after is not None:
                delay = max(delay, retry_after)
            await sleep(delay)


async def send_event(url, token, event, *, ca_file=None):
    """Acknowledge only a committed write or an explicitly cancelled raw event.

    A retry replays the same event ID, so a lost response cannot add a second
    event or a second memory.
    """
    result = await _replay(
        lambda: _event_request(url, token, event, ca_file=ca_file, path="/events")
    )
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
    try:
        result = await _replay(
            lambda: _event_request(
                url,
                token,
                {"action": action, "event_id": event_id},
                ca_file=ca_file,
                path="/events/manage",
            )
        )
    except RemoteStatusError as exc:
        if exc.error_type is None:
            raise
        # Keep the same envelope as every other client failure. The Owner type
        # is more useful to an operator than the local exception class.
        return {"error": _machine_failure(exc.error_type, exc.retryable, exc.retry_after)}
    error = _control_error(result)
    if error is not None:
        return {"error": _machine_failure(error["error"]["type"], error["error"]["retryable"])}
    if not isinstance(result, dict) or not isinstance(result.get("status"), str):
        raise ValueError("Invalid lifecycle control response")
    return result


async def call_remote(url, token, name, arguments, *, ca_file=None):
    """Use official protocol negotiation and preserve Owner receipt/error semantics.

    A retry replays the same arguments. Every shared mutation carries a
    ``request_id``, and the Owner returns the stored receipt for a repeat, so an
    interrupted response cannot duplicate a memory.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    validate_endpoint(url)
    validate_arguments(name, arguments, shared_schemas())
    if not isinstance(token, str) or not 32 <= len(token) <= 1000 or not token.isascii():
        raise ValueError("A provisioned service credential is required")
    http = http_module()
    context = ssl.create_default_context(cafile=ca_file)

    async def reject_unsafe_response(response):
        """Keep the HTTP status that the SDK may hide in a later stream error.

        A redirect and a credential rejection are final. A saturation answer is
        transient, so it keeps its own delay for the caller's outbox.
        """
        if 300 <= response.status_code < 400:
            raise PermissionError("Remote MCP redirects are forbidden")
        if response.status_code in (401, 403):
            raise RemoteStatusError(response.status_code)
        if response.status_code in RETRYABLE_STATUSES:
            raise RemoteStatusError(
                response.status_code,
                retryable=True,
                retry_after=_parse_retry_after(response.headers.get("Retry-After")),
            )

    async def request():
        async with http.AsyncClient(
            verify=context,
            trust_env=False,
            follow_redirects=False,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"Authorization": "Bearer " + token},
            event_hooks={"response": [reject_unsafe_response]},
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
                    error = value.get("error")
                    if isinstance(error, dict) and error.get("retryable") is True:
                        # The Owner itself reported a transient failure, so this
                        # request may not have reached its ledger entry yet.
                        raise RemoteStatusError(
                            None,
                            error_type=error.get("type"),
                            retryable=True,
                        )
                    return value

    return await _replay(request)


HINTS = {
    "tls_verification_failed": "Trust the Owner CA file with --ca-file; never disable verification.",
    "tls_failed": "Check the Owner certificate chain and the TLS listener.",
    "authentication_rejected": "Rotate or reissue the device credential; confirm the Owner policy.",
    "recovery_required": "The Owner keeps this request; finish operator recovery, then replay the same ID.",
    "unreachable": "Confirm the Owner address, listener and network path; replay the same request ID.",
    "timeout": "Confirm the Owner is responsive, then replay the same request ID.",
    "outcome_unknown": "Replay the same request ID; the Owner returns the original receipt.",
    "temporarily_unavailable": "Retry the same request ID after a short delay.",
    "protocol_error": "Confirm the endpoint ends in /mcp and serves the expected MCP version.",
    "business_error": "Read the Owner error type; the request reached the Owner.",
}


def _exception_leaves(exc, limit=64):
    """Flatten nested exception groups so one transport leaf is not hidden."""
    leaves, pending = [], [exc]
    while pending and len(leaves) < limit:
        node = pending.pop(0)
        if isinstance(node, BaseExceptionGroup) and node.exceptions:
            pending.extend(node.exceptions)
        else:
            leaves.append(node)
    return leaves or [exc]


def _exception_chain(exc, limit=8):
    """Follow cause and context links without looping on a self reference."""
    chain, node = [], exc
    while node is not None and node not in chain and len(chain) < limit:
        chain.append(node)
        node = node.__cause__ or node.__context__
    return chain


def _transport_types(name):
    """Collect one exception class from each supported HTTP client module."""
    modules = []
    try:
        import httpx

        modules.append(httpx)
    except ImportError:  # pragma: no cover - httpx ships with the remote extra
        return ()
    try:
        alternate = http_module()
    except ImportError:  # the optional MCP transport is not installed
        alternate = None
    if alternate is not None and alternate is not httpx:
        modules.append(alternate)
    return tuple(getattr(module, name, ()) for module in modules)


def remote_error(exc, replay=None):
    """Return a stable transport diagnostic without remote text or credentials.

    ``reason`` is the only thing a caller should branch on. ``retryable`` means
    the request can be replayed with its original request ID or event ID, and
    ``replay`` names that ID field so the caller cannot invent a second one.
    """
    leaves = _exception_leaves(exc)
    chain = [item for leaf in leaves for item in _exception_chain(leaf)]
    status, error_type = None, None
    owner_retry, retry_after = None, None
    for item in chain:
        if retry_after is None:
            retry_after = getattr(item, "retry_after", None)
        if status is None:
            status = getattr(item, "status_code", None)
            if status is None:
                status = getattr(getattr(item, "response", None), "status_code", None)
        if isinstance(item, RemoteStatusError) and item.error_type is not None:
            error_type = error_type or item.error_type
            owner_retry = item.retryable if owner_retry is None else owner_retry
    reason = "remote_error"
    try:
        if any(isinstance(item, ssl.SSLCertVerificationError) for item in chain):
            reason = "tls_verification_failed"
        elif any(isinstance(item, ssl.SSLError) for item in chain):
            reason = "tls_failed"
        elif error_type == "SharedRecoveryRequiredError":
            reason = "recovery_required"
        elif status in (401, 403):
            reason = "authentication_rejected"
        elif owner_retry is True or (owner_retry is None and status in RETRYABLE_STATUSES):
            reason = "temporarily_unavailable"
        elif status is not None and 300 <= status < 400:
            reason = "protocol_error"
        elif status is not None:
            reason = "business_error"
        elif error_type is not None:
            reason = "business_error"
        elif any(isinstance(leaf, PermissionError) for leaf in leaves):
            reason = "protocol_error"
        elif any(
            isinstance(leaf, _transport_types("RemoteProtocolError"))
            or isinstance(leaf, _transport_types("ReadError"))
            or isinstance(leaf, _transport_types("WriteError"))
            or isinstance(leaf, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError))
            for leaf in leaves
        ):
            reason = "outcome_unknown"
        elif any(_stream_closed(leaf) for leaf in leaves):
            reason = "outcome_unknown"
        elif any(
            isinstance(leaf, _transport_types("ConnectError"))
            or isinstance(leaf, _transport_types("ConnectTimeout"))
            for leaf in leaves
        ):
            reason = "unreachable"
        elif any(
            isinstance(leaf, _transport_types("TimeoutException") + (TimeoutError,))
            for leaf in leaves
        ):
            reason = "timeout"
        elif any(isinstance(leaf, ConnectionError) for leaf in leaves):
            reason = "unreachable"
        elif any(
            type(leaf).__name__ in {"McpError", "MCPError", "BrokenResourceError"}
            for leaf in leaves
        ):
            reason = "protocol_error"
    except ImportError:  # pragma: no cover - httpx ships with the remote extra
        pass
    result = {
        "type": type(leaves[0]).__name__,
        "reason": reason,
        "retryable": reason in RETRYABLE_REASONS,
        "hint": HINTS.get(reason, "Inspect the Owner log with the correlation identifier."),
    }
    if result["retryable"]:
        if replay is not None:
            result["replay"] = replay
        if retry_after is not None:
            result["retry_after"] = retry_after
    return result


def _stream_closed(exc):
    """Recognise an interrupted MCP stream without importing anyio at module load."""
    try:
        import anyio

        if isinstance(
            exc,
            (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream),
        ):
            return True
    except (ImportError, AttributeError):  # pragma: no cover - anyio ships with MCP
        pass
    # SDK 1.x names the class McpError and SDK 2.x names it MCPError.
    if type(exc).__name__ not in {"McpError", "MCPError"}:
        return False
    error = getattr(exc, "error", None)
    code = getattr(error, "code", None)
    if code is None:
        code = getattr(exc, "code", None)
    try:
        from mcp.types import CONNECTION_CLOSED

        return code == CONNECTION_CLOSED
    except ImportError:  # pragma: no cover - MCP is optional
        return code == -32000


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
