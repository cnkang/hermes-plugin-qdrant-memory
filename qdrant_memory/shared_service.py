"""Opt-in loopback private API: one Runtime owner, authenticated fixed-space clients."""

import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from threading import BoundedSemaphore

from .models import Scope, content_hash, digest, enforce_limits, now, payload, point_id
from .retry import safe_error
from .shared_state import SharedRecoveryRequiredError
from .tools import schemas

LOG = logging.getLogger(__name__)
MARKER = "qdrant-memory-shared-v1"


class SharedConflictError(RuntimeError):
    """Reject a stale revision or reuse of a request ID for different input."""


def shared_schemas():
    """Extend only the opt-in tool surface with durable retry and revision contracts."""
    result = schemas()
    for item in result:
        if item["name"].endswith("search"):
            continue
        fields = item["parameters"]
        fields["properties"]["request_id"] = {"type": "string"}
        fields["required"].append("request_id")
        if item["name"].endswith(("update", "delete")):
            fields["properties"]["expected_version"] = {"type": "string"}
            fields["required"].append("expected_version")
    return result


def _validate_grant(grant, seen):
    """Validate a single fixed identity before comparing space boundaries."""
    if not isinstance(grant, dict) or set(grant) != {
        "token_sha256",
        "principal",
        "owner_id",
        "space_id",
        "permission",
        "visibility",
    }:
        raise ValueError("Invalid shared grant fields")
    if not all(isinstance(value, str) and value and len(value) <= 256 for value in grant.values()):
        raise ValueError("Invalid shared grant values")
    if not re.fullmatch("[a-f0-9]{64}", grant["token_sha256"]) or grant["token_sha256"] in seen:
        raise ValueError("Credential hashes must be unique SHA256 values")
    seen.add(grant["token_sha256"])
    if grant["permission"] not in {"read", "write"} or grant["visibility"] not in {
        "private",
        "shared",
        "project",
    }:
        raise ValueError("Invalid shared grant permission")


def load_policy(path):
    """Read explicit operator grants; credential hashes are never tool arguments."""
    path = Path(path)
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise PermissionError("Shared policy must be private to its owner")
    policy = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(policy, dict)
        or set(policy) != {"clients"}
        or not isinstance(policy["clients"], list)
        or not policy["clients"]
    ):
        raise ValueError("Expected explicit shared clients")
    seen = set()
    spaces = {}
    for grant in policy["clients"]:
        _validate_grant(grant, seen)
        space = (grant["owner_id"], grant["space_id"])
        boundary = (
            grant["visibility"],
            grant["principal"] if grant["visibility"] == "private" else None,
        )
        if space in spaces and spaces[space] != boundary:
            raise ValueError("Space visibility/ownership cannot differ between grants")
        spaces[space] = boundary
    return policy["clients"]


class SharedOwner:
    """Authorize every operation and coordinate durable plans under the Runtime lock."""

    def __init__(self, runtime, grants):
        """Recover prepared requests before accepting any new client work."""
        self.runtime, self.grants = runtime, grants
        self.blocked = False
        ledger = runtime.ledger
        with ledger.lock, ledger.db:
            ledger.db.execute("""CREATE TABLE IF NOT EXISTS shared_requests_v1 (
                collection TEXT NOT NULL, request_key TEXT NOT NULL, input_hash TEXT NOT NULL,
                plan TEXT NOT NULL, response TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(collection, request_key))""")
        with runtime.lock:
            rows = ledger.db.execute(
                "SELECT * FROM shared_requests_v1 WHERE collection=? AND done=0 ORDER BY rowid",
                (ledger.collection,),
            ).fetchall()
            for row in rows:
                self._apply(row["request_key"], json.loads(row["plan"]))

    def authenticate(self, token):
        """Resolve identity only from a configured credential; never from request data."""
        if not isinstance(token, str) or not 32 <= len(token) <= 1000:
            LOG.info("shared_access principal=unknown decision=deny")
            raise PermissionError("Invalid shared credential")
        hashed = hashlib.sha256(token.encode()).hexdigest()
        for grant in self.grants:
            if hmac.compare_digest(hashed, grant["token_sha256"]):
                return grant
        LOG.info("shared_access principal=unknown decision=deny")
        raise PermissionError("Unknown shared credential")

    @staticmethod
    def scope(grant):
        """Map stable owner and space to an exact reserved agent partition."""
        return Scope(grant["owner_id"], "shared:v1:" + grant["space_id"])

    @staticmethod
    def belongs(value, grant):
        """Legacy records remain private even if their old scope matches a new space."""
        return (
            value.get("shared_protocol") == MARKER
            and value.get("space_id") == grant["space_id"]
            and value.get("visibility") == grant["visibility"]
        )

    def call(self, token, name, args):
        """Audit allow/deny without credentials, text, query, IDs or exception messages."""
        grant = self.authenticate(token)
        outcome = "deny"
        try:
            with self.runtime.lock:
                result = self._call(grant, name, args)
            outcome = "allow"
            return result
        finally:
            LOG.info("shared_access principal=%s decision=%s", digest(grant["principal"]), outcome)

    @staticmethod
    def _validate_arguments(name, args):
        """Check the exact shared catalogue before resolving any memory scope."""
        definition = next((item for item in shared_schemas() if item["name"] == name), None)
        if definition is None or not isinstance(args, dict):
            raise ValueError("Unknown shared tool")
        fields = definition["parameters"]
        if set(args) - set(fields["properties"]) or set(fields["required"]) - set(args):
            raise ValueError("Invalid shared tool arguments")
        if any(not isinstance(v, str) or not v or len(v.encode()) > 65536 for v in args.values()):
            raise ValueError("Invalid shared tool values")

    def _search(self, grant, scope, args):
        """Read only the authenticated space and explicit shared marker."""
        filters = {
            "shared_protocol": MARKER,
            "space_id": grant["space_id"],
            "visibility": grant["visibility"],
        }
        if args.get("category"):
            filters["category"] = args["category"]
        return {
            "memories": [
                {"id": str(hit.id), "score": hit.score, **hit.payload}
                for hit in self.runtime.store.search(args["query"], scope, filters)
            ]
        }

    def _call(self, grant, name, args):
        self._validate_arguments(name, args)
        scope = self.scope(grant)
        if name.endswith("search"):
            return self._search(grant, scope, args)
        if grant["permission"] != "write":
            raise PermissionError("Read-only shared credential")
        if self.blocked:
            raise SharedRecoveryRequiredError()
        if len(args["request_id"]) > 128:
            raise ValueError("Request ID too long")
        request_key = digest(
            [grant["owner_id"], grant["space_id"], grant["principal"], args["request_id"]]
        )
        input_hash = digest([name, args])
        ledger = self.runtime.ledger
        row = ledger.db.execute(
            "SELECT * FROM shared_requests_v1 WHERE collection=? AND request_key=?",
            (ledger.collection, request_key),
        ).fetchone()
        if row:
            if row["input_hash"] != input_hash:
                raise SharedConflictError("Request ID was already used")
            if not row["done"]:
                raise SharedRecoveryRequiredError()
            return json.loads(row["response"])
        plan, result = self._plan(grant, scope, name, args)
        # A complete immutable plan is durable BEFORE enqueueing a Runtime operation.
        # Restart can recreate the same operation even if killed between these steps.
        with ledger.lock, ledger.db:
            ledger.db.execute(
                "INSERT INTO shared_requests_v1 VALUES(?,?,?,?,?,0)",
                (
                    ledger.collection,
                    request_key,
                    input_hash,
                    json.dumps(plan),
                    json.dumps(result),
                ),
            )
        try:
            self._apply(request_key, plan)
        except BaseException:
            self.blocked = True
            raise
        return result

    def _validate_existing_add(self, old, grant, text):
        """Never promote private collisions or reuse an edited content ID."""
        if not self.belongs(old.payload, grant):
            raise PermissionError("Existing private ID cannot become shared")
        if old.payload["content_hash"] != content_hash(text):
            raise SharedConflictError("Original content ID has since been edited")

    def _plan(self, grant, scope, name, args):
        source_id, source_version = "", ""
        if name.endswith("add"):
            identifier = point_id(scope, MARKER, content_hash(args["text"]))
            old = self.runtime.store.lookup_hash(
                scope, content_hash(args["text"])
            ) or self.runtime.store.get(identifier, scope)
            if old:
                identifier = str(old.id)
                self._validate_existing_add(old, grant, args["text"])
                return None, {"id": identifier, "action": "SKIP", "version": old.payload["version"]}
            value = payload(args["text"], scope, MARKER)
            creator = grant["principal"]
        else:
            identifier = args["id"]
            old = self.runtime.store.get(identifier, scope)
            if old is None or not self.belongs(old.payload, grant):
                raise PermissionError("Memory ID unavailable in caller space")
            if old.payload["version"] != args["expected_version"]:
                raise SharedConflictError("Stale memory version")
            if name.endswith("delete"):
                source_id = self.runtime.ledger.delete_fence_source(scope)
                source_version = now()
                return {
                    "identifier": identifier,
                    "action": "DELETE",
                    "value": {"content_hash": old.payload["content_hash"]},
                    "source_id": source_id,
                    "source_version": source_version,
                }, {"id": identifier, "action": "DELETE", "version": old.payload["version"]}
            value = {
                **old.payload,
                "text": args["text"],
                "content_hash": content_hash(args["text"]),
                "updated_at": now(),
            }
            creator = old.payload["created_by"]
        version = str(uuid.uuid4())  # Opaque revision avoids delete/recreate ABA conflicts.
        value.update(
            shared_protocol=MARKER,
            space_id=grant["space_id"],
            visibility=grant["visibility"],
            created_by=creator,
            updated_by=grant["principal"],
            version=version,
        )
        value = enforce_limits(value, self.runtime.cfg["limits"])
        return {
            "identifier": identifier,
            "action": "UPSERT",
            "value": value,
            "source_id": source_id,
            "source_version": source_version,
        }, {
            "id": identifier,
            "action": "ADD" if name.endswith("add") else "UPDATE",
            "version": version,
        }

    def _apply(self, request_key, plan):
        if plan is not None:
            key = self.runtime.operation(**plan)
            self.runtime.commit([key])
            if self.runtime.ledger.row("operations", key)["status"] != "COMMITTED":
                raise SharedRecoveryRequiredError()
        ledger = self.runtime.ledger
        with ledger.lock, ledger.db:
            ledger.db.execute(
                "UPDATE shared_requests_v1 SET done=1,plan='null' WHERE collection=? AND request_key=?",
                (ledger.collection, request_key),
            )


def _error_status(exc):
    """Map sanitized service failures to explicit transport outcomes."""
    if isinstance(exc, PermissionError):
        return 403
    if isinstance(exc, SharedConflictError):
        return 409
    if isinstance(exc, SharedRecoveryRequiredError):
        return 503
    return 400


def make_server(owner, port):
    """Bind IPv4 loopback only; this bounded private API is not HTTP MCP."""

    class Handler(BaseHTTPRequestHandler):
        """Accept authenticated JSON POSTs and reject browser/cross-origin access."""

        def setup(self):
            """Bound idle connection lifetime before parsing any request."""
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            """Never log HTTP headers, credentials, paths or request bodies."""

        def do_POST(self):
            """Validate transport boundaries before resolving a trusted identity."""
            status, result = 200, None
            try:
                if (
                    self.path != "/v1/tool"
                    or self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}"
                    or self.headers.get("Origin") is not None
                    or self.headers.get("Transfer-Encoding") is not None
                    or self.headers.get("Content-Type") != "application/json"
                ):
                    raise PermissionError("Invalid local request boundary")
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer ") or len(auth) > 1024:
                    raise PermissionError("Credential required")
                owner.authenticate(auth[7:])
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 131072:
                    raise ValueError("Invalid request size")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict) or set(data) != {"name", "arguments"}:
                    raise ValueError("Invalid request envelope")
                result = owner.call(auth[7:], data["name"], data["arguments"])
            except Exception as exc:
                status = _error_status(exc)
                result = {"error": safe_error(exc)}
            body = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass  # Durable admission is independent of the response connection.

    class BoundedServer(ThreadingHTTPServer):
        """Bound workers while retaining drain-before-lease-release semantics."""

        slots = BoundedSemaphore(32)

        def server_bind(self):
            """Bind the literal loopback without HTTPServer's reverse DNS lookup."""
            TCPServer.server_bind(self)
            self.server_name = "127.0.0.1"
            self.server_port = self.server_address[1]

        def process_request(self, request, client_address):
            """Apply admission backpressure before creating another worker."""
            self.slots.acquire()
            try:
                super().process_request(request, client_address)
            except BaseException:
                self.slots.release()
                raise

        def process_request_thread(self, request, client_address):
            """Release admission only after a request has completely finished."""
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.slots.release()

    # Non-daemon threads are joined by server_close before the Runtime lease exits.
    return BoundedServer(("127.0.0.1", port), Handler)


def serve(home, policy_path, port):
    """Hold ownership until every admitted request drains, including disconnected clients."""
    from .standalone import open_runtime

    grants = load_policy(policy_path)
    if not LOG.handlers:
        LOG.addHandler(logging.StreamHandler())
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    with open_runtime(home, shared_owner=True) as runtime:
        owner = SharedOwner(runtime, grants)
        with make_server(owner, port) as server:
            print(
                json.dumps({"ready": True, "url": f"http://127.0.0.1:{server.server_port}"}),
                flush=True,
            )
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
