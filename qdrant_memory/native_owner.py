"""Opt-in Owner hosted by the existing Native Provider and host LLM Runtime."""

from pathlib import Path
from threading import Thread

from .config import secret
from .shared_service import SharedOwner, load_policy, load_policy_document, make_server


def load_native_settings(home, value):
    """Validate an explicit local Owner role before opening any private resources."""
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("enabled", False), bool):
        raise ValueError("Invalid native_owner configuration")
    if not value.get("enabled", False):
        return None
    if set(value) - {
        "enabled",
        "policy",
        "port",
        "private_token_env",
        "shared_capture_token_envs",
        "remote_port",
        "allowed_hosts",
    }:
        raise ValueError("Unsupported native_owner setting")
    policy = Path(value.get("policy", ""))
    if not policy.is_absolute():
        policy = Path(home) / policy
    from .private_paths import checked_absolute_path

    policy = checked_absolute_path(policy.absolute())
    if not policy.is_relative_to(Path(home).resolve()):
        raise ValueError("Native Owner policy must be inside its private home")
    port = value.get("port", 8765)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("Invalid Native Owner loopback port")
    names = [value.get("private_token_env"), *value.get("shared_capture_token_envs", [])]
    import re

    if not all(
        isinstance(name, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", name) for name in names
    ):
        raise ValueError("Native Owner requires credential environment names")
    document = load_policy_document(policy)
    if "remote_port" in value:
        remote_port = value["remote_port"]
        if (
            isinstance(remote_port, bool)
            or not isinstance(remote_port, int)
            or not 0 <= remote_port <= 65535
            or document.get("destination") is None
        ):
            raise ValueError("Native remote MCP requires a protected policy and a loopback port")
    return {**value, "home": Path(home), "policy": policy, "document": document, "port": port}


class NativeOwner:
    """Share one host-owned Runtime; never acquire a second store, lease or Ledger."""

    def __init__(self, runtime, settings, user_id):
        """Recover requests and resolve the explicitly selected Native private grant."""
        self.settings = settings
        binding = settings["document"].get("destination")

        def admission():
            from .shared_security import validate_destination

            validate_destination(settings["home"], runtime.cfg, binding)

        self.owner = SharedOwner(
            runtime,
            settings["document"]["clients"],
            grant_loader=lambda: load_policy(settings["policy"]),
            admission_check=admission if binding is not None else None,
        )
        grant = self.owner.authenticate(secret(settings["private_token_env"]))
        if (
            grant["owner_id"] != user_id
            or grant["visibility"] != "private"
            or grant["permission"] != "write"
            or "logical_agent_id" not in grant
        ):
            raise ValueError("Native Owner requires its own writable device-bound private grant")
        self.scope = self.owner.scope(grant)
        self.server = self.thread = None
        self.remote = self.remote_thread = self.remote_socket = None
        self.url = None

    def start(self):
        """Bind loopback only after recovery and complete initialization."""
        self.server = make_server(self.owner, self.settings["port"])
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = Thread(target=self.server.serve_forever, name="native-memory-owner")
        self.thread.start()
        if "remote_port" in self.settings:
            import socket
            import time
            from contextvars import copy_context

            import uvicorn

            from .remote_gateway import create_gateway

            app = create_gateway(self.owner, allowed_hosts=self.settings.get("allowed_hosts", []))
            self.remote_socket = socket.socket()
            self.remote_socket.bind(("127.0.0.1", self.settings["remote_port"]))
            self.remote_socket.listen(128)
            self.remote = uvicorn.Server(
                uvicorn.Config(
                    app,
                    proxy_headers=False,
                    access_log=False,
                    server_header=False,
                    log_level="critical",
                    limit_concurrency=64,
                )
            )
            context = copy_context()
            self.remote_thread = Thread(
                target=lambda: context.run(self.remote.run, sockets=[self.remote_socket]),
                name="native-memory-mcp",
            )
            self.remote_thread.start()
            deadline = time.monotonic() + 10
            while (
                not self.remote.started
                and self.remote_thread.is_alive()
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            if not self.remote.started:
                raise RuntimeError("Native HTTP MCP listener failed startup")

    def close(self):
        """Drain every request before the Native worker closes its Runtime resources."""
        if self.remote is not None:
            self.remote.should_exit = True
            if self.remote_thread is not None:
                self.remote_thread.join()
            self.remote = None
        if self.remote_socket is not None:
            self.remote_socket.close()
            self.remote_socket = None
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()
            self.server = None

    def token(self):
        """Resolve the current scoped Native credential; never expose it in diagnostics."""
        return secret(self.settings["private_token_env"])

    def events(self, value, *, include_shared=True):
        """Bind new permitted turns to private and explicitly approved project spaces."""
        if value["scope"] != self.scope.as_dict():
            return [value]
        names = [self.settings["private_token_env"]]
        if include_shared and value["kind"] == "turn":
            names += self.settings.get("shared_capture_token_envs", [])
        result = []
        for index, name in enumerate(names):
            grant = self.owner.authenticate(secret(name))
            if not index and self.owner.scope(grant) != self.scope:
                raise PermissionError(
                    "Native private identity changed; explicit reconfiguration required"
                )
            if (
                grant["owner_id"] != self.scope.user_id
                or grant["permission"] != "write"
                or "logical_agent_id" not in grant
                or (
                    index
                    and (
                        grant["visibility"] not in {"project", "shared"}
                        or not grant.get("capture_rule_id")
                    )
                )
            ):
                raise PermissionError("Native automatic capture rule is unavailable")
            authority = {
                key: grant[key]
                for key in (
                    "principal",
                    "owner_id",
                    "space_id",
                    "visibility",
                    "logical_agent_id",
                    "device_id",
                )
            }
            authority["capture_rule_id"] = grant.get("capture_rule_id", "private-default")
            result.append(
                {
                    **value,
                    "source": "owner_lifecycle",
                    "scope": self.owner.scope(grant).as_dict(),
                    "owner_authority": authority,
                }
            )
        return result

    def reauthorize_pending(self):
        """After explicit operator retry, recover unadmitted turns into private only."""
        import json

        ledger = self.owner.runtime.ledger
        for row in ledger.iter_rows("events"):
            value = json.loads(row["payload_json"])
            if value.get("source") != "native_owner_unadmitted":
                continue
            if value.get("scope") != self.scope.as_dict():
                raise PermissionError("Pending Native event belongs to another logical identity")
            captured = self.events(value, include_shared=False)[0]
            ledger.prepare_event(row["event_id"], captured)
