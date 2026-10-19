"""Official SDK HTTP transport with credential-scoped Owner dispatch."""

import asyncio
import inspect
import json

import httpx
import pytest

from qdrant_memory.remote_gateway import create_gateway, create_shared_server
from qdrant_memory.shared_service import SharedOwner

from ..helpers import runtime
from .test_device_grants import grant


def test_remote_factory_has_no_private_runtime_parameter():
    """A remote server cannot accidentally fall back to private runtime dispatch."""
    assert set(inspect.signature(create_shared_server).parameters) == {"owner", "token_resolver"}


def test_real_sdk_http_crud_isolation_and_request_authentication(tmp_path):
    """Exercise actual MCP messages, not a private JSON API or a fake Owner."""
    rt = runtime(tmp_path)
    a, b = "a" * 40, "b" * 40
    ga = grant(a, "mac")
    gb = grant(b, "codex", logical="codex", space="codex-private")
    owner = SharedOwner(rt, [ga, gb])
    app = create_gateway(owner, allowed_hosts=["memory.test"])

    async def check():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://memory.test"
            ) as client:

                async def rpc(token, method, params=None, **headers):
                    return await client.post(
                        "/mcp",
                        headers={
                            "Authorization": "Bearer " + token,
                            "Accept": "application/json, text/event-stream",
                            **headers,
                        },
                        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                    )

                init = {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "contract", "version": "1"},
                }
                assert (await rpc(a, "initialize", init)).status_code == 200
                tools = (await rpc(a, "tools/list")).json()["result"]["tools"]
                assert len(tools) == 4
                assert (
                    "request_id"
                    in next(t for t in tools if t["name"].endswith("add"))["inputSchema"][
                        "required"
                    ]
                )
                saved_response = await rpc(
                    a,
                    "tools/call",
                    {
                        "name": "qdrant_memory_add",
                        "arguments": {"text": "Project Alpha uses SQLite WAL.", "request_id": "r1"},
                    },
                )
                result = saved_response.json()["result"]
                assert not result.get("isError")
                saved = json.loads(result["content"][0]["text"])
                replay = await rpc(
                    a,
                    "tools/call",
                    {
                        "name": "qdrant_memory_add",
                        "arguments": {"text": "Project Alpha uses SQLite WAL.", "request_id": "r1"},
                    },
                )
                assert json.loads(replay.json()["result"]["content"][0]["text"]) == saved
                search = await rpc(
                    b,
                    "tools/call",
                    {"name": "qdrant_memory_search", "arguments": {"query": "Alpha"}},
                )
                assert json.loads(search.json()["result"]["content"][0]["text"])["memories"] == []
                assert (await rpc("invalid", "tools/list")).status_code == 401
                ga["revoked"] = True
                assert (await rpc(a, "tools/list")).status_code == 401
                assert (await rpc(b, "tools/list", Origin="https://evil.test")).status_code == 403
                assert (await rpc(b, "tools/list", Host="evil.test")).status_code == 403
                assert (
                    await rpc(b, "tools/list", **{"X-Forwarded-Host": "memory.test"})
                ).status_code == 403

    try:
        asyncio.run(check())
    finally:
        rt.ledger.close()
        rt.store.close()


def test_empty_or_wildcard_host_allowlist_rejected(tmp_path):
    """There is no permissive public gateway default."""
    rt = runtime(tmp_path)
    try:
        for hosts in ([], ["*"], ["memory.test:*"], ["https://memory.test"]):
            with pytest.raises(ValueError):
                create_gateway(SharedOwner(rt, []), allowed_hosts=hosts)
    finally:
        rt.ledger.close()
        rt.store.close()


def test_lifecycle_route_opt_in_and_bounded_event_admission(tmp_path):
    """The extension shares authentication and does not add a fifth MCP CRUD tool."""
    from qdrant_memory.owner_lifecycle import OwnerLifecycle

    rt = runtime(tmp_path)
    token = "a" * 40
    owner = SharedOwner(rt, [grant(token, "mac")])
    app = create_gateway(owner, allowed_hosts=["memory.test"], lifecycle=OwnerLifecycle(owner))

    async def check():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://memory.test"
            ) as client:
                event = {
                    "event_id": "http-event",
                    "session_id": "s",
                    "kind": "turn_completed",
                    "user": "cats durable",
                    "assistant": "ack",
                }
                headers = {"Authorization": "Bearer " + token}
                assert (await client.post("/events", json=event, headers=headers)).json()[
                    "status"
                ] == "COMMITTED"
                assert (await client.post("/events", json=event)).status_code == 403
                assert (
                    await client.post("/events", json={**event, "scope": "spoof"}, headers=headers)
                ).status_code == 400
                assert (
                    await client.post("/events", content=b"x" * 131073, headers=headers)
                ).status_code == 413

    try:
        asyncio.run(check())
    finally:
        rt.ledger.close()
        rt.store.close()
