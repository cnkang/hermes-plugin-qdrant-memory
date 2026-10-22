"""Opt-in loopback private API: one Runtime owner, authenticated fixed-space clients."""

import hashlib
import hmac
import json
import logging
import math
import os
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from threading import BoundedSemaphore

from .models import (
    Scope,
    content_hash,
    digest,
    enforce_limits,
    normalize,
    now,
    payload,
    point_id,
)
from .retry import safe_error
from .shared_protocol import MARKER, VISIBILITIES
from .shared_state import SharedRecoveryRequiredError
from .tools import schemas, validate_arguments

LOG = logging.getLogger(__name__)


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
    required = {
        "token_sha256",
        "principal",
        "owner_id",
        "space_id",
        "permission",
        "visibility",
    }
    optional = {"logical_agent_id", "device_id", "expires_at", "revoked", "capture_rule_id"}
    if (
        not isinstance(grant, dict)
        or not required <= set(grant)
        or set(grant) - required - optional
    ):
        raise ValueError("Invalid shared grant fields")
    strings = required | ({"logical_agent_id", "device_id"} & set(grant))
    if not all(isinstance(grant[k], str) and grant[k] and len(grant[k]) <= 256 for k in strings):
        raise ValueError("Invalid shared grant values")
    if ("logical_agent_id" in grant) != ("device_id" in grant):
        raise ValueError("Logical identity and device must be configured together")
    if "capture_rule_id" in grant and (
        "logical_agent_id" not in grant
        or not isinstance(grant["capture_rule_id"], str)
        or not 0 < len(grant["capture_rule_id"]) <= 256
    ):
        raise ValueError("An audited capture rule requires a device identity")
    if "revoked" in grant and not isinstance(grant["revoked"], bool):
        raise ValueError("Revocation must be boolean")
    if "expires_at" in grant:
        expiry = grant["expires_at"]
        if (
            isinstance(expiry, bool)
            or not isinstance(expiry, (int, float))
            or not math.isfinite(expiry)
            or expiry <= 0
        ):
            raise ValueError("Expiry must be a finite positive Unix timestamp")
    if not re.fullmatch("[a-f0-9]{64}", grant["token_sha256"]) or grant["token_sha256"] in seen:
        raise ValueError("Credential hashes must be unique SHA256 values")
    seen.add(grant["token_sha256"])
    if grant["permission"] not in {"read", "write"} or grant["visibility"] not in VISIBILITIES:
        raise ValueError("Invalid shared grant permission")


def load_policy(path):
    """Read explicit operator grants; credential hashes are never tool arguments."""
    return load_policy_document(path)["clients"]


def load_policy_document(path):
    """Keep legacy grants compatible while accepting an explicit destination binding."""
    path = Path(path)
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise PermissionError("Shared policy must be private to its owner")
    policy = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(policy, dict)
        or set(policy) not in ({"clients"}, {"clients", "destination"})
        or not isinstance(policy["clients"], list)
        or not policy["clients"]
    ):
        raise ValueError("Expected explicit shared clients")
    seen = set()
    spaces = {}
    devices = {}
    principals = {}
    for grant in policy["clients"]:
        _validate_grant(grant, seen)
        if "device_id" in grant:
            device = (grant["owner_id"], grant["device_id"])
            identity = (grant["principal"], grant["logical_agent_id"])
            if device in devices and devices[device] != identity:
                raise ValueError("Device identity cannot differ between grants")
            devices[device] = identity
            principal = (grant["owner_id"], grant["principal"])
            if principal in principals and principals[principal] != (
                grant["device_id"],
                grant["logical_agent_id"],
            ):
                raise ValueError("A principal cannot impersonate another device identity")
            principals[principal] = (grant["device_id"], grant["logical_agent_id"])
        space = (grant["owner_id"], grant["space_id"])
        boundary = (
            grant["visibility"],
            (
                ("logical", grant["logical_agent_id"])
                if "logical_agent_id" in grant
                else ("legacy", grant["principal"])
            )
            if grant["visibility"] == "private"
            else None,
        )
        if space in spaces and spaces[space] != boundary:
            raise ValueError("Space visibility/ownership cannot differ between grants")
        spaces[space] = boundary
    if "destination" in policy:
        from .shared_security import private_entry

        private_entry(path)
        if not isinstance(policy["destination"], dict):
            raise ValueError("Expected a protected destination binding")
    return policy


class SharedOwner:
    """Authorize every operation and coordinate durable plans under the Runtime lock."""

    def __init__(
        self, runtime, grants, *, admission_check=None, grant_loader=None, execution_context=None
    ):
        """Recover prepared requests before accepting any new client work."""
        self.runtime, self.grants = runtime, grants
        self.admission_check = admission_check
        self.grant_loader = grant_loader
        self.execution_context = execution_context
        self.blocked = False
        ledger = runtime.ledger
        with ledger.lock, ledger.db:
            ledger.db.execute("""CREATE TABLE IF NOT EXISTS shared_requests_v1 (
                collection TEXT NOT NULL, request_key TEXT NOT NULL, input_hash TEXT NOT NULL,
                plan TEXT NOT NULL, response TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
                principal TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(collection, request_key))""")
            # Additive upgrade: existing profiles keep their receipts and gain
            # attribution for requests admitted after this revision.
            columns = {row[1] for row in ledger.db.execute("PRAGMA table_info(shared_requests_v1)")}
            if "principal" not in columns:
                ledger.db.execute(
                    "ALTER TABLE shared_requests_v1 ADD COLUMN principal TEXT NOT NULL DEFAULT ''"
                )
        with runtime.lock:
            rows = ledger.db.execute(
                "SELECT * FROM shared_requests_v1 WHERE collection=? AND done=0 ORDER BY rowid",
                (ledger.collection,),
            ).fetchall()
            for row in rows:
                try:
                    self._apply(row["request_key"], json.loads(row["plan"]))
                except SharedConflictError:
                    # A superseded plan is a complete outcome (receipt recorded);
                    # keep replaying the remaining pending requests.
                    continue

    def authenticate(self, token):
        """Resolve identity only from a configured credential; never from request data."""
        if not isinstance(token, str) or not 32 <= len(token) <= 1000:
            LOG.info("shared_access principal=unknown decision=deny")
            raise PermissionError("Invalid shared credential")
        hashed = hashlib.sha256(token.encode()).hexdigest()
        try:
            grants = self.grant_loader() if self.grant_loader is not None else self.grants
        except Exception:
            raise PermissionError("Shared policy unavailable") from None
        for grant in grants:
            if hmac.compare_digest(hashed, grant["token_sha256"]):
                if grant.get("revoked", False) or time.time() >= grant.get(
                    "expires_at", float("inf")
                ):
                    raise PermissionError("Shared credential unavailable")
                return grant
        LOG.info("shared_access principal=unknown decision=deny")
        raise PermissionError("Unknown shared credential")

    @staticmethod
    def scope(grant):
        """Map stable owner and space to an exact reserved agent partition."""
        if grant["visibility"] == "private" and "logical_agent_id" in grant:
            return Scope(
                grant["owner_id"],
                "shared:v2:" + digest([grant["space_id"], grant["logical_agent_id"]]),
            )
        return Scope(grant["owner_id"], "shared:v1:" + grant["space_id"])

    @staticmethod
    def belongs(value, grant):
        """Legacy records remain private even if their old scope matches a new space."""
        return (
            value.get("shared_protocol") == MARKER
            and value.get("space_id") == grant["space_id"]
            and value.get("visibility") == grant["visibility"]
            and (
                grant["visibility"] != "private"
                or "logical_agent_id" not in grant
                or value.get("logical_agent_id") == grant["logical_agent_id"]
            )
        )

    @staticmethod
    def boundary(grant):
        """Return the exact marker/space/visibility filter set for one grant."""
        result = {
            "shared_protocol": MARKER,
            "space_id": grant["space_id"],
            "visibility": grant["visibility"],
        }
        if grant["visibility"] == "private" and "logical_agent_id" in grant:
            result["logical_agent_id"] = grant["logical_agent_id"]
        return result

    @staticmethod
    def point(scope, grant, source, text):
        """Derive a shared point ID inside the grant's visibility boundary.

        Older records used the same scope and content hash without the visibility
        suffix; the filtered hash lookup still finds them, so this only prevents a
        later visibility generation from colliding with the previous one.
        """
        return point_id(scope, source + ":" + grant["visibility"], content_hash(text))

    def call(self, token, name, args):
        """Audit allow/deny without credentials, text, query, IDs or exception messages."""
        if self.execution_context is not None:
            return self.execution_context.copy().run(self._authorized_call, token, name, args)
        return self._authorized_call(token, name, args)

    def _authorized_call(self, token, name, args):
        """Run under the owning Host profile context when the Owner is Native."""
        grant = None
        outcome = "deny"
        try:
            with self.runtime.lock:
                grant = self.authenticate(token)
                result = self._call(grant, name, args)
            outcome = "allow"
            return result
        finally:
            LOG.info(
                "shared_access principal=%s decision=%s",
                digest(grant["principal"]) if grant else "unknown",
                outcome,
            )

    @staticmethod
    def _validate_arguments(name, args):
        """Check the exact shared catalogue before resolving any memory scope."""
        validate_arguments(name, args, shared_schemas())
        if any(not value for value in args.values()):
            raise ValueError("Invalid shared tool values")

    def _search(self, grant, scope, args):
        """Read only the authenticated space and explicit shared marker."""
        filters = self.boundary(grant)
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
        if self.admission_check is not None:
            self.admission_check()
        if self.blocked:
            raise SharedRecoveryRequiredError()
        if len(args["request_id"]) > 128:
            raise ValueError("Request ID too long")
        identity = [grant["owner_id"], grant["space_id"], grant["principal"]]
        if "logical_agent_id" in grant:
            identity.extend([grant["logical_agent_id"], grant["device_id"]])
        request_key = digest([*identity, args["request_id"]])
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
                "INSERT INTO shared_requests_v1"
                "(collection,request_key,input_hash,principal,plan,response,done)"
                " VALUES(?,?,?,?,?,?,0)",
                (
                    ledger.collection,
                    request_key,
                    input_hash,
                    grant["principal"],
                    json.dumps(plan),
                    json.dumps(result),
                ),
            )
        try:
            self._apply(request_key, plan)
        except SharedConflictError:
            # A superseded plan is a determinate outcome with a recorded
            # receipt; it must not block the service.
            raise
        except BaseException:
            self.blocked = True
            raise
        return result

    def _validate_existing_add(self, old, grant, text):
        """Never promote private collisions or reuse an edited content ID."""
        if not self.belongs(old.payload, grant):
            raise PermissionError("Existing private ID cannot become shared")
        stored = enforce_limits(
            {"text": text, "metadata": {}, "content_hash": content_hash(text)},
            self.runtime.cfg["limits"],
        )["content_hash"]
        if old.payload["content_hash"] not in {content_hash(text), stored}:
            raise SharedConflictError("Original content ID has since been edited")

    def _plan(self, grant, scope, name, args):
        source_id, source_version = "", ""
        if name.endswith("add"):
            identifier = self.point(scope, grant, MARKER, args["text"])
            old = self.runtime.store.lookup_hash(
                scope, content_hash(args["text"]), filters=self.boundary(grant)
            )
            # A same-partition record from another visibility generation is outside
            # this caller's boundary, not a conflict: ignore it and keep the new ID.
            if old is None:
                candidate = self.runtime.store.get(identifier, scope)
                old = (
                    candidate
                    if candidate is not None and self.belongs(candidate.payload, grant)
                    else None
                )
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
            if not normalize(args["text"]):
                raise ValueError("Memory text must be nonempty")
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
        if "device_id" in grant:
            value.update(
                logical_agent_id=grant["logical_agent_id"],
                created_by_device=value.get("created_by_device", grant["device_id"]),
                updated_by_device=grant["device_id"],
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
            status = self.runtime.ledger.row("operations", key)["status"]
            if status == "SUPERSEDED":
                # A newer operation replaced this one before it committed. The
                # outcome is determinate: complete the receipt with a recorded
                # non-retryable conflict so the caller can re-read, without
                # blocking the service on a benign race.
                ledger = self.runtime.ledger
                conflict = {"error": safe_error(SharedConflictError("Operation was superseded"))}
                with ledger.lock, ledger.db:
                    ledger.db.execute(
                        "UPDATE shared_requests_v1 SET done=1,plan='null',response=?"
                        " WHERE collection=? AND request_key=?",
                        (json.dumps(conflict), ledger.collection, request_key),
                    )
                raise SharedConflictError("Operation was superseded")
            if status != "COMMITTED":
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

        def version_string(self):
            """Never fingerprint the Python runtime in the Server header."""
            return "qdrant-memory"

        def discard_rejected_body(self):
            """Bound a rejected-body drain so Windows can receive the denial response."""
            lengths = self.headers.get_all("Content-Length", [])
            if (
                self.headers.get("Transfer-Encoding") is not None
                or len(lengths) != 1
                or re.fullmatch(r"[0-9]{1,6}", lengths[0]) is None
            ):
                return
            size = int(lengths[0])
            # One extra byte covers the size-limit probe. Never parse or admit
            # rejected data; bound the whole drain, including slow repeated reads.
            if 0 < size <= 131073:
                previous_timeout = self.connection.gettimeout()
                deadline = time.monotonic() + 1
                try:
                    while size:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self.connection.settimeout(remaining)
                        chunk = self.rfile.read1(min(size, 8192))
                        if not chunk:
                            break
                        size -= len(chunk)
                except OSError:
                    pass
                finally:
                    self.connection.settimeout(previous_timeout)

        def do_POST(self):
            """Validate transport boundaries before resolving a trusted identity."""
            status, result, body_read = 200, None, False
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
                body_read = True
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict) or set(data) != {"name", "arguments"}:
                    raise ValueError("Invalid request envelope")
                result = owner.call(auth[7:], data["name"], data["arguments"])
            except Exception as exc:
                if not body_read:
                    self.discard_rejected_body()
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
        """Bound workers while keeping the accept loop available to every peer."""

        daemon_threads = False
        block_on_close = True

        def __init__(self, *args, **kwargs):
            self.slots = BoundedSemaphore(32)
            super().__init__(*args, **kwargs)

        def server_bind(self):
            """Bind the literal loopback without HTTPServer's reverse DNS lookup."""
            TCPServer.server_bind(self)
            self.server_name = "127.0.0.1"
            self.server_port = self.server_address[1]

        def process_request(self, request, client_address):
            """Refuse overflow in-band instead of parking the accept loop.

            Taking the permit here with a blocking acquire lets an unauthenticated
            peer stop every later connection from being read at all. Refusing the
            connection keeps admission bounded while the listener stays responsive.
            """
            if not self.slots.acquire(blocking=False):
                body = b'{"error":{"type":"SharedAdmissionError","retryable":true}}'
                try:
                    request.sendall(
                        b"HTTP/1.0 429 Too Many Requests\r\n"
                        b"Retry-After: 1\r\nContent-Type: application/json\r\n"
                        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                        b"Connection: close\r\n\r\n" + body
                    )
                    # Drain only bytes already buffered, without waiting on the
                    # accept loop. A later segment may reset the connection.
                    request.setblocking(False)
                    while True:
                        try:
                            if not request.recv(65536):
                                break
                        except OSError:
                            break
                except OSError:
                    pass
                finally:
                    self.shutdown_request(request)
                return
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

    policy = load_policy_document(policy_path)
    binding = policy.get("destination")
    if not LOG.handlers:
        LOG.addHandler(logging.StreamHandler())
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    previous_umask = os.umask(0o077) if binding is not None else None
    try:
        with open_runtime(home, shared_owner=True, destination_policy=binding) as runtime:

            def admission_check():
                from .shared_security import private_entry, validate_destination

                private_entry(policy_path)
                validate_destination(home, runtime.cfg, binding)

            owner = SharedOwner(
                runtime,
                policy["clients"],
                admission_check=admission_check if binding is not None else None,
                grant_loader=lambda: load_policy(policy_path),
            )
            with make_server(owner, port) as server:
                print(
                    json.dumps({"ready": True, "url": f"http://127.0.0.1:{server.server_port}"}),
                    flush=True,
                )
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
    finally:
        if previous_umask is not None:
            os.umask(previous_umask)
