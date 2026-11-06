"""Opt-in Streamable HTTP on loopback, behind a same-host HTTPS proxy.

Only pre-provisioned service credentials are supported. This is not an OAuth
authorization server, and it must never be advertised as OAuth interoperability.
"""

import re
import time
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

from .mcp_server import create_tool_server
from .shared_service import shared_schemas

LIFECYCLE_CONTROL_PATH = "/events/manage"


@contextmanager
def _graceful_sigterm():
    """Let uvicorn's re-raised SIGTERM unwind the Owner's drain/resource stack."""
    import signal
    import threading

    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def terminate(signum, frame):
        raise SystemExit(0)

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


class _LifecycleBodyTooLarge(Exception):
    """A lifecycle request exceeded the bounded body size."""


async def _read_lifecycle_body(receive, limit=131072):
    """Read one bounded ASGI body; ``None`` means the peer disconnected."""
    body = bytearray()
    while True:
        chunk = await receive()
        if chunk["type"] == "http.disconnect":
            return None
        body.extend(chunk.get("body", b""))
        if len(body) > limit:
            raise _LifecycleBodyTooLarge()
        if not chunk.get("more_body", False):
            return bytes(body)


async def _lifecycle_dispatch(lifecycle, token, scope, event):
    """Route one parsed event to the Owner lifecycle through a fixed schema."""
    import anyio

    if scope["path"] == LIFECYCLE_CONTROL_PATH:
        if (
            not isinstance(event, dict)
            or set(event) != {"action", "event_id"}
            or not isinstance(event["action"], str)
            or event["action"] not in {"status", "retry", "cancel"}
        ):
            raise ValueError("Invalid lifecycle control")
        method = {
            "status": lifecycle.inspect,
            "retry": lifecycle.retry,
            "cancel": lifecycle.cancel,
        }[event["action"]]
        return await anyio.to_thread.run_sync(lambda: method(token, event["event_id"]))
    return await anyio.to_thread.run_sync(lambda: lifecycle.submit(token, event))


async def _lifecycle_request(lifecycle, token, scope, receive, send):
    """Bound the optional event extension and keep its authority entirely Owner-side."""
    import json

    from starlette.responses import Response

    from .retry import safe_error

    try:
        body = await _read_lifecycle_body(receive)
    except _LifecycleBodyTooLarge:
        await Response(status_code=413)(scope, receive, send)
        return
    if body is None:
        return
    try:
        result = await _lifecycle_dispatch(lifecycle, token, scope, json.loads(body))
        status = 200
    except Exception as exc:
        result = {"error": safe_error(exc)}
        status = 400
        if isinstance(exc, PermissionError):
            status = 403
        elif result["error"].get("retryable"):
            status = 503
    await Response(json.dumps(result), status_code=status, media_type="application/json")(
        scope, receive, send
    )


def create_shared_server(owner, token_resolver):
    """Expose only credential-bound Owner tools, with no private Runtime fallback."""
    return create_tool_server(
        shared_schemas,
        lambda name, arguments: owner.call(token_resolver(), name, arguments),
    )


def _validate_allowed_hosts(allowed_hosts):
    """Refuse an empty or wildcard Host authority list before binding."""
    if not allowed_hosts or any(
        not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.-]+(?::\d{1,5})?", host)
        for host in allowed_hosts
    ):
        raise ValueError("Explicit HTTP Host authorities are required; wildcards are forbidden")


class _GatewayState:
    """Hold the single-Owner transport wiring shared by the ASGI helpers."""

    def __init__(self, owner, allowed_hosts, lifecycle):
        """Build one session manager, one admission pool and the token context."""
        import anyio
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from mcp.server.transport_security import TransportSecuritySettings

        self.owner = owner
        self.allowed_hosts = allowed_hosts
        self.lifecycle = lifecycle
        self.current_token = ContextVar("qdrant_memory_remote_credential")
        self.server = create_shared_server(owner, self.current_token.get)
        self.manager = StreamableHTTPSessionManager(
            self.server,
            stateless=True,
            json_response=True,
            max_request_body_size=131072,
            security_settings=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=list(allowed_hosts),
                allowed_origins=[],
            ),
        )
        self.slots = anyio.Semaphore(32)
        self.buckets = {}
        self.auth_limiter = anyio.CapacityLimiter(8)

    @asynccontextmanager
    async def lifespan(self, app):
        """Start and stop the session manager with the ASGI application."""
        async with self.manager.run():
            yield


def _unsafe_headers(scope):
    """Report query strings, proxy metadata and duplicated security headers."""
    names = [name.lower() for name, _ in scope["headers"]]
    # The local TLS proxy strips these; client-controlled proxy metadata has
    # no authority here. Duplicate security headers are ambiguous and denied.
    if scope.get("query_string"):
        return True
    if any(name in {b"origin", b"forwarded"} or name.startswith(b"x-forwarded-") for name in names):
        return True
    return any(names.count(name) != 1 for name in (b"host", b"authorization"))


async def _boundary_allowed(state, scope, receive, send):
    """Check header and Host boundaries; return ``False`` after the denial."""
    from starlette.responses import Response

    if _unsafe_headers(scope):
        await Response(status_code=403)(scope, receive, send)
        return False
    values = dict(scope["headers"])
    if values.get(b"host", b"").decode("latin-1") not in state.allowed_hosts:
        await Response(status_code=403)(scope, receive, send)
        return False
    return True


async def _current_grant(state, scope, receive, send):
    """Resolve the bearer credential; return ``(None, None)`` after the denial."""
    import anyio
    from starlette.responses import Response

    auth = dict(scope["headers"]).get(b"authorization", b"")
    try:
        if not auth.startswith(b"Bearer ") or len(auth) > 1024:
            raise PermissionError()
        token = auth[7:].decode("ascii")
        # Even initialize and tools/list require a current credential.
        grant = await anyio.to_thread.run_sync(
            state.owner.authenticate, token, limiter=state.auth_limiter
        )
    except Exception:
        await Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})(
            scope, receive, send
        )
        return None, None
    return grant, token


def _prune_buckets(state, now):
    """Drop fully idle per-principal buckets before admitting a new request."""
    stale = [
        key
        for key, (times, slots) in state.buckets.items()
        if times and times[-1] < now - 60 and slots.value == 4
    ]
    for old_key in stale:
        del state.buckets[old_key]


def _arrivals(state, key, now):
    """Return the trailing minute window and slot semaphore for one principal."""
    import anyio

    arrivals, principal_slots = state.buckets.setdefault(key, (deque(), anyio.Semaphore(4)))
    while arrivals and arrivals[0] < now - 60:
        arrivals.popleft()
    return arrivals, principal_slots


async def _admitted(state, scope, receive, send, grant):
    """Acquire admission permits; return a release callback, or ``None`` on 429."""
    import anyio
    from starlette.responses import Response

    key = (grant["owner_id"], grant["principal"])
    now = time.monotonic()
    _prune_buckets(state, now)
    arrivals, principal_slots = _arrivals(state, key, now)
    if len(arrivals) >= 240:
        await Response(status_code=429, headers={"Retry-After": "60"})(scope, receive, send)
        return None
    arrivals.append(now)
    try:
        principal_slots.acquire_nowait()
    except anyio.WouldBlock:
        await Response(status_code=429, headers={"Retry-After": "1"})(scope, receive, send)
        return None
    try:
        state.slots.acquire_nowait()
    except anyio.WouldBlock:
        principal_slots.release()
        await Response(status_code=429, headers={"Retry-After": "1"})(scope, receive, send)
        return None

    def release():
        """Return both admission permits exactly once."""
        state.slots.release()
        principal_slots.release()

    return release


async def _gateway_authorized(state, scope, receive, send, token):
    """Dispatch one authenticated POST to the lifecycle or MCP surface."""
    from starlette.responses import Response

    if scope["method"] != "POST":
        await Response(status_code=405, headers={"Allow": "POST"})(scope, receive, send)
        return
    if scope["path"] in {"/events", LIFECYCLE_CONTROL_PATH} and state.lifecycle is not None:
        await _lifecycle_request(state.lifecycle, token, scope, receive, send)
        return
    context = state.current_token.set(token)
    try:
        await state.manager.handle_request(scope, receive, send)
    finally:
        state.current_token.reset(context)


async def _gateway_endpoint(state, scope, receive, send):
    """Authenticate one request, admit it and dispatch it inside the permit."""
    if not await _boundary_allowed(state, scope, receive, send):
        return
    grant, token = await _current_grant(state, scope, receive, send)
    if grant is None:
        return
    release = await _admitted(state, scope, receive, send, grant)
    if release is None:
        return
    try:
        await _gateway_authorized(state, scope, receive, send, token)
    finally:
        release()


class _GatewayEndpoint:
    """Keep raw ASGI streams intact instead of wrapping a request handler."""

    def __init__(self, state):
        """Bind the shared gateway state to one route endpoint."""
        self._state = state

    async def __call__(self, scope, receive, send):
        """Bound each receive so an idle peer cannot hold a worker forever."""
        import anyio

        async def bounded_receive():
            with anyio.fail_after(10):
                return await receive()

        await _gateway_endpoint(self._state, scope, bounded_receive, send)


def create_gateway(owner, *, allowed_hosts, lifecycle=None):
    """Require explicit authority values and authenticate every HTTP request."""
    from starlette.applications import Starlette
    from starlette.routing import Route

    _validate_allowed_hosts(allowed_hosts)
    state = _GatewayState(owner, allowed_hosts, lifecycle)
    endpoint = _GatewayEndpoint(state)
    routes = [Route("/mcp", endpoint=endpoint, methods=["POST", "GET", "DELETE"])]
    if lifecycle is not None:
        routes.append(Route("/events", endpoint=endpoint, methods=["POST"]))
        routes.append(Route(LIFECYCLE_CONTROL_PATH, endpoint=endpoint, methods=["POST"]))
    return Starlette(
        routes=routes,
        lifespan=state.lifespan,
    )


LISTENER_OPTIONS = {
    "http": "qdrant_memory.remote_protocol:HeaderTimeoutH11Protocol",
    "proxy_headers": False,
    "access_log": False,
    "server_header": False,
    "log_level": "critical",
    "timeout_keep_alive": 5,
}


def listener_socket(port):
    """Bind the loopback MCP listener so a restart can reuse the port.

    Accepted connections leave the address in TIME_WAIT after a normal stop. On
    POSIX the option keeps a supervisor or operator restart from failing with
    EADDRINUSE without letting two live listeners share one port. Windows treats
    the option as permission to share a live port, so it is not set there.
    """
    import os
    import socket

    sock = socket.socket()
    if os.name != "nt":
        # Windows interprets SO_REUSEADDR as permission to share a live port, so
        # the option stays POSIX-only. Protected service surfaces are unsupported
        # on Windows and fail closed there.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(128)
    return sock


# No listener-wide limit_concurrency: uvicorn counts accepted connections before a
# request exists, so a local peer could otherwise turn idle sockets into a global 503
# that bypasses the authenticated per-principal gates below.


def serve(home, policy_path, *, port, local_port, allowed_hosts, enable_lifecycle=False):
    """Run both listeners in one Owner process and drain before releasing storage."""
    import json
    from contextlib import ExitStack
    from threading import Thread

    import uvicorn

    from .shared_service import SharedOwner, load_policy, load_policy_document, make_server
    from .standalone import open_runtime

    policy = load_policy_document(policy_path)
    binding = policy.get("destination")
    if binding is None:
        raise ValueError("Remote Owner requires a protected destination policy")
    with ExitStack() as resources:
        extractor = None
        if enable_lifecycle:
            from .extraction import Extractor
            from .owner_llm import OwnerLLM
            from .standalone import load_standalone_config

            cfg = load_standalone_config(home)
            llm = OwnerLLM(cfg["llm"])
            resources.callback(llm.close)
            extractor = Extractor(llm, cfg["llm"])
        runtime = resources.enter_context(
            open_runtime(home, shared_owner=True, destination_policy=binding, extractor=extractor)
        )

        def admission_check():
            from .shared_security import private_entry, validate_destination

            private_entry(policy_path)
            validate_destination(home, runtime.cfg, binding)

        owner = SharedOwner(
            runtime,
            policy["clients"],
            admission_check=admission_check,
            grant_loader=lambda: load_policy(policy_path),
        )
        lifecycle = None
        if enable_lifecycle:
            from .owner_lifecycle import OwnerLifecycle

            lifecycle = OwnerLifecycle(owner)
            if lifecycle.recover():
                raise RuntimeError("Owner lifecycle recovery must finish before admission")
        app = create_gateway(owner, allowed_hosts=allowed_hosts, lifecycle=lifecycle)
        with make_server(owner, local_port) as local, listener_socket(port) as sock:
            thread = Thread(target=local.serve_forever, name="qdrant-memory-loopback")
            thread.start()
            try:
                config = uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=port,
                    **LISTENER_OPTIONS,
                )
                service = uvicorn.Server(config)
                print(
                    json.dumps(
                        {
                            "bound": True,
                            "local_port": local.server_port,
                            "remote_port": sock.getsockname()[1],
                            "transport": "streamable-http",
                            "tls": "same-host proxy required",
                        }
                    ),
                    flush=True,
                )
                with _graceful_sigterm():
                    service.run(sockets=[sock])
                    if not service.started:
                        raise RuntimeError("Remote MCP listener failed startup")
            finally:
                local.shutdown()
                thread.join()
