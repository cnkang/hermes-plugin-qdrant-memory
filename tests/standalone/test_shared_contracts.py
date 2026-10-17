"""Shared owner security boundaries measured directly alongside process acceptance."""

import json
from contextlib import contextmanager
from threading import Thread

import httpx
import pytest

from qdrant_memory.shared_client import call
from qdrant_memory.shared_service import SharedOwner, load_policy, make_server
from qdrant_memory.shared_state import SharedRecoveryRequiredError
from qdrant_memory.standalone import open_runtime

from .test_shared_service import TOKENS, policy
from .test_standalone import command
from .test_standalone import profile as standalone_profile


def test_loopback_startup_does_not_depend_on_dns(monkeypatch):
    """An unavailable resolver must never block literal-loopback service readiness."""

    def unavailable_resolver(*args):
        """Fail immediately if server binding attempts reverse hostname resolution."""
        raise AssertionError("Literal loopback must not invoke DNS")

    monkeypatch.setattr("socket.getfqdn", unavailable_resolver)
    with make_server(None, 0) as server:
        assert server.server_name == "127.0.0.1"
        assert server.server_address[0] == "127.0.0.1"
        assert server.server_port > 0


@pytest.fixture
def profile(tmp_path):
    """Reuse the hermetic embedding transport without inheriting unrelated fixtures."""
    yield from standalone_profile.__wrapped__(tmp_path)


@contextmanager
def live_owner(profile):
    """Exercise real embedded storage and HTTP with the measured owner in this process."""
    command(profile, "init")
    home, _ = profile
    with open_runtime(home, shared_owner=True) as runtime:
        owner = SharedOwner(runtime, load_policy(policy(home)))
        with make_server(owner, 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                yield owner, f"http://127.0.0.1:{server.server_port}"
            finally:
                server.shutdown()
                thread.join(timeout=10)
                assert not thread.is_alive()


def test_owner_authorization_revisions_and_durable_receipts(profile):
    """Reject forged scopes and stale writes while reusing durable receipts exactly."""
    with live_owner(profile) as (owner, url):

        def invoke(name, **args):
            return call(url, TOKENS["hermes"], "qdrant_memory_" + name, args)

        added = invoke("add", text="shared fact", request_id="create")
        assert added["action"] == "ADD"
        assert invoke("add", text="shared fact", request_id="create") == added
        duplicate = invoke("add", text="shared fact", request_id="deduplicate")
        assert duplicate["action"] == "SKIP"
        assert duplicate["version"] == added["version"]
        assert not invoke("search", query="fact", category="unmatched-category")["memories"]
        hits = invoke("search", query="fact")["memories"]
        assert hits[0]["id"] == added["id"]
        assert (
            invoke("add", text="other", request_id="create")["error"]["type"]
            == "SharedConflictError"
        )
        updated = invoke(
            "update",
            id=added["id"],
            text="changed fact",
            expected_version=added["version"],
            request_id="update",
        )
        assert updated["action"] == "UPDATE"
        assert (
            invoke(
                "update",
                id=added["id"],
                text="stale",
                expected_version=added["version"],
                request_id="stale",
            )["error"]["type"]
            == "SharedConflictError"
        )
        assert (
            invoke("add", text="shared fact", request_id="edited-original")["error"]["type"]
            == "SharedConflictError"
        )
        for identity in ("reader", "private", "foreign"):
            result = call(
                url,
                TOKENS[identity],
                "qdrant_memory_delete",
                {"id": added["id"], "expected_version": updated["version"], "request_id": identity},
            )
            assert result["error"]["type"] == "PermissionError"
        assert invoke("search", query="fact", owner_id="foreign")["error"]["type"] == "ValueError"
        assert invoke("add", text=5, request_id="wrong-type")["error"]["type"] == "ValueError"
        assert invoke("add", text="x", request_id="x" * 129)["error"]["type"] == "ValueError"
        assert invoke("unknown")["error"]["type"] == "ValueError"
        owner.blocked = True
        assert (
            invoke("add", text="blocked", request_id="blocked")["error"]["type"]
            == "SharedRecoveryRequiredError"
        )
        owner.blocked = False
        deleted = invoke(
            "delete", id=added["id"], expected_version=updated["version"], request_id="delete"
        )
        assert deleted["action"] == "DELETE"
        assert not invoke("search", query="fact")["memories"]
        assert (
            invoke(
                "delete", id=added["id"], expected_version=updated["version"], request_id="missing"
            )["error"]["type"]
            == "PermissionError"
        )
        # An admitted but unfinished receipt cannot be reported as successful.
        owner.runtime.ledger.db.execute(
            "UPDATE shared_requests_v1 SET done=0 WHERE request_key IS NOT NULL"
        )
        owner.runtime.ledger.db.commit()
        with pytest.raises(SharedRecoveryRequiredError):
            owner.call(
                TOKENS["hermes"],
                "qdrant_memory_add",
                {"text": "shared fact", "request_id": "create"},
            )


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:80",
        "http://localhost:80",
        "http://example.com:80",
        "http://127.0.0.1",
        "http://127.0.0.1:80/other",
        "http://user@127.0.0.1:80",
        "http://127.0.0.1:80?x=1",
        "http://127.0.0.1:80#x",
        "http://127.0.0.1.evil:80",
    ],
)
def test_client_refuses_untrusted_destinations(url):
    """Validate before opening any connection or transmitting a credential."""
    with pytest.raises(ValueError, match="literal IPv4 loopback"):
        call(url, TOKENS["dsh"], "qdrant_memory_search", {"query": "x"})


def test_client_requires_credential_before_connecting():
    """A malformed credential never reaches the network."""
    with pytest.raises(ValueError, match="high-entropy"):
        call("http://127.0.0.1:1", "short", "qdrant_memory_search", {})


@pytest.mark.parametrize(
    "mutation", ["origin", "host", "path", "content-type", "auth", "size", "envelope"]
)
def test_http_boundary_rejects_untrusted_requests(profile, mutation):
    """Transport rejects browser, routing, credential and envelope substitution."""
    with live_owner(profile) as (_, url):
        headers = {"Authorization": "Bearer " + TOKENS["dsh"], "Content-Type": "application/json"}
        data = {"name": "qdrant_memory_search", "arguments": {"query": "x"}}
        path = "/v1/tool"
        if mutation == "origin":
            headers["Origin"] = "http://untrusted.invalid"
        elif mutation == "host":
            headers["Host"] = "untrusted.invalid"
        elif mutation == "path":
            path = "/wrong"
        elif mutation == "content-type":
            headers["Content-Type"] = "text/plain"
        elif mutation == "auth":
            headers["Authorization"] = "Bearer unknown"
        elif mutation == "envelope":
            data["owner_id"] = "forged"
        body = "" if mutation == "size" else json.dumps(data)
        response = httpx.post(url + path, headers=headers, content=body, trust_env=False)
        assert response.status_code in {400, 403}
        assert set(response.json()) == {"error"}
        assert TOKENS["dsh"] not in response.text


@pytest.mark.parametrize(
    "change",
    [
        "envelope",
        "fields",
        "value",
        "hash",
        "duplicate",
        "permission",
        "visibility",
        "private-owner",
    ],
)
def test_policy_rejects_invalid_or_ambiguous_grants(tmp_path, change):
    """Reject malformed policy and conflicting identities before acquiring the Runtime."""
    path = policy(tmp_path)
    data = json.loads(path.read_text())
    grant = data["clients"][0]
    if change == "envelope":
        data = []
    elif change == "fields":
        grant["extra"] = "x"
    elif change == "value":
        grant["principal"] = 1
    elif change == "hash":
        grant["token_sha256"] = "bad"
    elif change == "duplicate":
        data["clients"].append(dict(grant))
    elif change == "permission":
        grant["permission"] = "admin"
    elif change == "visibility":
        grant["visibility"] = "global"
    else:
        grant["visibility"] = "private"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_policy(path)
