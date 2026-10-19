"""Synthetic two-network TLS MCP acceptance; no host profiles or production keys."""

import asyncio
import hashlib
import json
import os
import ssl
import subprocess
import sys
from contextlib import ExitStack
from importlib.metadata import version
from pathlib import Path

import httpx
import uvicorn
from qdrant_client import QdrantClient

sys.path.insert(0, "/app")

from qdrant_memory.owner_lifecycle import OwnerLifecycle  # noqa: E402
from qdrant_memory.ownership import WriterLease  # noqa: E402
from qdrant_memory.remote_client import call_remote  # noqa: E402
from qdrant_memory.remote_gateway import create_gateway  # noqa: E402
from qdrant_memory.shared_service import SharedOwner, load_policy  # noqa: E402
from tests.helpers import config, runtime  # noqa: E402

TOKENS = {
    name: hashlib.sha256(("synthetic-https-" + name).encode()).hexdigest()
    for name in (
        "hermes-a",
        "hermes-b",
        "codex",
        "reader",
        "private-a",
        "private-b",
        "codex-private",
    )
}


def serve():
    """Keep TLS termination and loopback Owner on one isolated network instance."""
    os.umask(0o077)
    home = Path("/tmp/owner-state")
    home.mkdir(exist_ok=True, mode=0o700)
    tls = Path("/tmp/tls")
    tls.mkdir(exist_ok=True)
    cert, key = tls / "cert.pem", tls / "key.pem"
    if not cert.exists():
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=owner",
                "-addext",
                "subjectAltName=DNS:owner",
                "-keyout",
                str(key),
                "-out",
                str(cert),
            ],
            check=True,
            capture_output=True,
        )
    grants = []
    for name, token in TOKENS.items():
        private = name in {"private-a", "private-b", "codex-private"}
        logical = (
            "hermes"
            if name.startswith(("hermes", "private"))
            else "codex"
            if name.startswith("codex")
            else name
        )
        grants.append(
            {
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "principal": name,
                "owner_id": "alice",
                "logical_agent_id": logical,
                "device_id": name,
                "space_id": logical + "-private" if private else "alpha",
                "visibility": "private" if private else "project",
                "permission": "read" if name == "reader" else "write",
            }
        )
        if name == "hermes-a":
            grants[-1]["capture_rule_id"] = "synthetic-user-approved-alpha"
    policy = home / "policy.json"
    # Preserve live revocation edits on restart.
    if not policy.exists():
        policy.write_text(json.dumps({"clients": grants}))
    cfg = config(qdrant={"path": str(home / "qdrant")})
    with ExitStack() as resources:
        lease = WriterLease.for_config(home, cfg)
        resources.callback(lease.close)
        rt = runtime(home, client=QdrantClient(path=str(home / "qdrant")), cfg=cfg)
        resources.callback(rt.store.close)
        resources.callback(rt.ledger.close)
        rt.recover()
        owner = SharedOwner(rt, load_policy(policy), grant_loader=lambda: load_policy(policy))
        lifecycle = OwnerLifecycle(owner)
        assert lifecycle.recover() == 0
        app = create_gateway(owner, allowed_hosts=["owner:8443"], lifecycle=lifecycle)
        nginx = home / "nginx.conf"
        nginx.write_text(f"""pid /tmp/nginx.pid;
events {{ worker_connections 128; }}
http {{ access_log off; error_log /dev/null crit;
server {{ listen 8443 ssl; server_name owner;
ssl_certificate {cert}; ssl_certificate_key {key};
client_max_body_size 128k; client_body_timeout 10s;
location ~ ^/(mcp|events)$ {{
if ($http_forwarded != '') {{ return 403; }}
if ($http_x_forwarded_host != '') {{ return 403; }}
proxy_set_header Host $http_host;
proxy_set_header Forwarded ''; proxy_set_header X-Forwarded-For '';
proxy_set_header X-Forwarded-Host ''; proxy_set_header X-Forwarded-Proto '';
proxy_pass http://127.0.0.1:8766; proxy_buffering off;
}} }} }}""")
        proxy = subprocess.Popen(["nginx", "-c", str(nginx), "-g", "daemon off;"])
        resources.callback(proxy.wait, timeout=10)
        resources.callback(proxy.terminate)
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=8766,
            proxy_headers=False,
            access_log=False,
            server_header=False,
            log_level="critical",
        )


async def client(recovery=False):
    """Run official SDK operations over verified TLS from a separate container."""
    url = "https://owner:8443/mcp"

    async def call(identity, name, args):
        return await call_remote(
            url, TOKENS[identity], "qdrant_memory_" + name, args, ca_file="/tmp/owner-ca.pem"
        )

    for _ in range(80):
        try:
            await call("hermes-a", "search", {"query": "ready"})
            break
        except Exception:
            await asyncio.sleep(0.1)
    else:
        raise RuntimeError("HTTPS Owner readiness failed")
    saved = await call(
        "hermes-a", "add", {"text": "Project Alpha uses SQLite WAL.", "request_id": "alpha"}
    )
    replay = await call(
        "hermes-a", "add", {"text": "Project Alpha uses SQLite WAL.", "request_id": "alpha"}
    )
    assert replay == saved
    found = await call("codex", "search", {"query": "Project Alpha journal mode"})
    row = next(item for item in found["memories"] if item["id"] == saved["id"])
    assert row["created_by"] == "hermes-a"
    if not recovery:
        assert row["version"] == saved["version"]
        assert row["text"] == "Project Alpha uses SQLite WAL."
    else:
        assert row["text"] == "Project Alpha uses SQLite WAL, confirmed."
    readonly = await call("reader", "add", {"text": "denied", "request_id": "ro"})
    assert readonly["error"]["type"] == "PermissionError"
    update = {
        "id": saved["id"],
        "text": "Project Alpha uses SQLite WAL, confirmed.",
        "expected_version": saved["version"],
        "request_id": "u1",
    }
    changed = await call("hermes-b", "update", update)
    assert changed["action"] == "UPDATE"
    stale = await call("codex", "update", {**update, "request_id": "u2"})
    assert stale["error"]["type"] == "SharedConflictError"
    assert await call("hermes-b", "update", update) == changed
    private = await call(
        "private-a", "add", {"text": "Hermes private cats", "request_id": "private"}
    )
    private_rows = await call("private-b", "search", {"query": "cats"})
    assert private["id"] in {item["id"] for item in private_rows["memories"]}
    assert not (await call("codex-private", "search", {"query": "cats"}))["memories"]
    assert private["id"] not in {item["id"] for item in found["memories"]}
    async with httpx.AsyncClient(
        verify=ssl.create_default_context(cafile="/tmp/owner-ca.pem"), trust_env=False
    ) as http:
        assert (
            await http.post(url, headers={"Authorization": "Bearer invalid"}, json={})
        ).status_code == 401
        assert (
            await http.post(
                url,
                headers={
                    "Authorization": "Bearer " + TOKENS["codex"],
                    "Origin": "https://evil.test",
                },
                json={},
            )
        ).status_code == 403
    # A fresh default trust store cannot accept this synthetic self-signed server.
    try:
        async with httpx.AsyncClient(trust_env=False) as http:
            await http.post(url, json={})
    except httpx.ConnectError:
        pass
    else:
        raise AssertionError("Untrusted TLS certificate was accepted")
    print(
        json.dumps(
            {
                "status": "PASS",
                "transport": "HTTPS Streamable HTTP",
                "backend": "Embedded",
                "sdk": version("mcp"),
                "network_instances": 2,
                "recovery": recovery,
                "checks": [
                    "official SDK",
                    "CRUD",
                    "shared recall",
                    "CAS",
                    "receipt",
                    "readonly",
                    "authentication",
                    "Origin",
                    "TLS trust",
                ],
            }
        )
    )


if __name__ == "__main__":
    if sys.argv[1] == "server":
        serve()
    else:
        asyncio.run(client(sys.argv[1] == "recover"))
