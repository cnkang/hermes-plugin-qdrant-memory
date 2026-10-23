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


async def _lifecycle_request(lifecycle, token, scope, receive, send):
    """Bound the optional event extension and keep its authority entirely Owner-side."""
    import json

    import anyio
    from starlette.responses import Response

    from .retry import safe_error

    body = bytearray()
    while True:
        chunk = await receive()
        if chunk["type"] == "http.disconnect":
            return
        body.extend(chunk.get("body", b""))
        if len(body) > 131072:
            await Response(status_code=413)(scope, receive, send)
            return
        if not chunk.get("more_body", False):
            break
    try:
        event = json.loads(body)
        if scope["path"] == "/events/manage":
            if (
                not isinstance(event, dict)
                or set(event) != {"action", "event_id"}
                or not isinstance(event["action"], str)
                or event["action"] not in {"status", "cancel"}
            ):
                raise ValueError("Invalid lifecycle control")
            method = lifecycle.inspect if event["action"] == "status" else lifecycle.cancel
            result = await anyio.to_thread.run_sync(lambda: method(token, event["event_id"]))
        else:
            result = await anyio.to_thread.run_sync(lambda: lifecycle.submit(token, event))
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


def create_gateway(owner, *, allowed_hosts, lifecycle=None):
    """Require explicit authority values and authenticate every HTTP request."""
    import anyio
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Route

    if not allowed_hosts or any(
        not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?", host)
        for host in allowed_hosts
    ):
        raise ValueError("Explicit HTTP Host authorities are required; wildcards are forbidden")
    current_token = ContextVar("qdrant_memory_remote_credential")
    server = create_shared_server(owner, current_token.get)
    manager = StreamableHTTPSessionManager(
        server,
        stateless=True,
        json_response=True,
        max_request_body_size=131072,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(allowed_hosts),
            allowed_origins=[],
        ),
    )
    slots = anyio.Semaphore(32)
    buckets = {}
    auth_limiter = anyio.CapacityLimiter(8)

    async def endpoint(scope, receive, send):
        headers = scope["headers"]
        names = [name.lower() for name, _ in headers]
        # The local TLS proxy strips these; client-controlled proxy metadata has
        # no authority here. Duplicate security headers are ambiguous and denied.
        if (
            scope.get("query_string")
            or any(
                name in {b"origin", b"forwarded"} or name.startswith(b"x-forwarded-")
                for name in names
            )
            or any(names.count(name) != 1 for name in (b"host", b"authorization"))
        ):
            await Response(status_code=403)(scope, receive, send)
            return
        values = dict(headers)
        if values.get(b"host", b"").decode("latin-1") not in allowed_hosts:
            await Response(status_code=403)(scope, receive, send)
            return
        auth = values.get(b"authorization", b"")
        try:
            if not auth.startswith(b"Bearer ") or len(auth) > 1024:
                raise PermissionError()
            token = auth[7:].decode("ascii")
            # Even initialize and tools/list require a current credential.
            grant = await anyio.to_thread.run_sync(owner.authenticate, token, limiter=auth_limiter)
        except Exception:
            await Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})(
                scope, receive, send
            )
            return
        key = (grant["owner_id"], grant["principal"])
        now = time.monotonic()
        for old_key in list(buckets):
            times, principal_slots = buckets[old_key]
            if times and times[-1] < now - 60 and principal_slots.value == 4:
                del buckets[old_key]
        arrivals, principal_slots = buckets.setdefault(key, (deque(), anyio.Semaphore(4)))
        while arrivals and arrivals[0] < now - 60:
            arrivals.popleft()
        if len(arrivals) >= 240:
            await Response(status_code=429, headers={"Retry-After": "60"})(scope, receive, send)
            return
        arrivals.append(now)
        try:
            principal_slots.acquire_nowait()
        except anyio.WouldBlock:
            await Response(status_code=429, headers={"Retry-After": "1"})(scope, receive, send)
            return
        try:
            slots.acquire_nowait()
        except anyio.WouldBlock:
            principal_slots.release()
            await Response(status_code=429, headers={"Retry-After": "1"})(scope, receive, send)
            return
        try:
            await authorized(scope, receive, send, token)
        finally:
            slots.release()
            principal_slots.release()

    async def authorized(scope, receive, send, token):
        if scope["method"] != "POST":
            await Response(status_code=405, headers={"Allow": "POST"})(scope, receive, send)
            return
        if scope["path"] in {"/events", "/events/manage"} and lifecycle is not None:
            await _lifecycle_request(lifecycle, token, scope, receive, send)
            return
        context = current_token.set(token)
        try:
            await manager.handle_request(scope, receive, send)
        finally:
            current_token.reset(context)

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    class Endpoint:
        """Keep raw ASGI streams intact instead of wrapping a request handler."""

        async def __call__(self, scope, receive, send):
            async def bounded_receive():
                with anyio.fail_after(10):
                    return await receive()

            await endpoint(scope, bounded_receive, send)

    routes = [Route("/mcp", endpoint=Endpoint(), methods=["POST", "GET", "DELETE"])]
    if lifecycle is not None:
        routes.append(Route("/events", endpoint=Endpoint(), methods=["POST"]))
        routes.append(Route("/events/manage", endpoint=Endpoint(), methods=["POST"]))
    return Starlette(
        routes=routes,
        lifespan=lifespan,
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
