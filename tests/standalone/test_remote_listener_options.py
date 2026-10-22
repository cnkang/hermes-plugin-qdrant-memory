"""The HTTPS MCP listener must not cap admission by accepted connections."""

import asyncio
import socket

import pytest


def test_remote_listener_has_no_pre_request_connection_cap():
    """Idle local sockets must not turn into a global 503 before authentication."""
    from qdrant_memory.remote_gateway import LISTENER_OPTIONS

    assert "limit_concurrency" not in LISTENER_OPTIONS


@pytest.mark.parametrize("case", ["idle", "partial", "keepalive-partial", "active"])
def test_listener_bounds_headers_without_timing_out_active_requests(case):
    """Real sockets expire before headers, including after a keep-alive response."""
    import uvicorn
    from uvicorn.importer import import_from_string
    from uvicorn.protocols.http.h11_impl import H11Protocol

    from qdrant_memory.remote_gateway import LISTENER_OPTIONS

    protocol = LISTENER_OPTIONS.get("http", H11Protocol)
    if isinstance(protocol, str):
        protocol = import_from_string(protocol)

    class FastProtocol(protocol):
        header_timeout_seconds = 0.05

    async def check():
        async def app(scope, receive, send):
            if scope["path"] == "/slow":
                await asyncio.sleep(0.15)
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-length", b"2")],
                }
            )
            await send({"type": "http.response.body", "body": b"ok"})

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(
            uvicorn.Config(app, lifespan="off", **{**LISTENER_OPTIONS, "http": FastProtocol})
        )
        task = asyncio.create_task(server.serve(sockets=[sock]))
        writer = None
        try:
            deadline = asyncio.get_running_loop().time() + 3
            while not server.started:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.005)
            reader, writer = await asyncio.open_connection("127.0.0.1", sock.getsockname()[1])
            if case in {"keepalive-partial", "active"}:
                path = "/slow" if case == "active" else "/"
                writer.write(f"GET {path} HTTP/1.1\r\nHost: memory.test\r\n\r\n".encode())
                await writer.drain()
                headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1)
                assert b"200 OK" in headers
                assert await asyncio.wait_for(reader.readexactly(2), 1) == b"ok"
                if case == "active":
                    return
            if case in {"partial", "keepalive-partial"}:
                writer.write(b"GET / HTTP/1.1\r\nHost:")
                await writer.drain()
            assert await asyncio.wait_for(reader.read(1), 0.5) == b""
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()
            server.should_exit = True
            await asyncio.wait_for(task, 3)
            sock.close()

    asyncio.run(check())
