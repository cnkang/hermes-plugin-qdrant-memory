"""Build one soak environment: TLS, real Qdrant, proxy, profile and policy.

The topology mirrors the documented deployment instead of inventing one:

    soak client --HTTPS--> nginx --HTTP--> Owner (native Python, loopback)
    Owner --HTTPS--> nginx --HTTP--> Qdrant server (Docker)
    optional: toxiproxy in front of either TLS listener for fault injection

Only the Owner and the harness are native processes, which keeps SIGTERM,
SIGKILL and restart behaviour real. Both TLS listeners bind IPv4 loopback with
the production header policy, so the soak exercises the documented HTTPS MCP
path rather than a shortcut.
"""

import contextlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .common import SECRETS, free_port, wait_until, write_json

QDRANT_IMAGE = (
    "qdrant/qdrant@sha256:0fb8897412abc81d1c0430a899b9a81eb8328aa634e7242d1bc804c1fe8fe863"
)
# Published multi-architecture manifest list (amd64 and arm64).
TOXIPROXY_IMAGE = "ghcr.io/shopify/toxiproxy@sha256:9378ed52a28bc50edc1350f936f518f31fa95f0d15917d6eb40b8e376d1a214e"

QDRANT_PROXY = "qdrant"
CLIENT_PROXY = "client"

# A deterministic grant matrix: one project space shared by three writers, two
# read-only credentials and two private partitions that must never overlap.
GRANT_SPECS = (
    # principal, logical agent, device, space, visibility, permission, capture rule
    ("hermes-a", "hermes", "laptop-a", "alpha", "project", "write", "soak-rule-alpha"),
    ("hermes-b", "hermes", "laptop-b", "alpha", "project", "write", "soak-rule-alpha"),
    ("hermes-c", "hermes", "laptop-c", "alpha", "project", "write", "soak-rule-alpha"),
    ("codex", "codex", "desk", "alpha", "project", "write", "soak-rule-codex"),
    ("codex-b", "codex", "desk-b", "alpha", "project", "write", "soak-rule-codex"),
    ("reader", "hermes", "reader", "alpha", "project", "read", None),
    ("burst", "hermes", "burst", "alpha", "project", "read", None),
    # The compare-and-swap probe owns two devices, so workload traffic can never
    # consume their request budget while the race is being measured.
    ("cas-a", "hermes", "cas-a", "alpha", "project", "write", "soak-rule-alpha"),
    ("cas-b", "codex", "cas-b", "alpha", "project", "write", "soak-rule-codex"),
    ("hermes-private", "hermes", "laptop-private", "hermes-private", "private", "write", None),
    ("codex-private", "codex", "desk-private", "codex-private", "private", "write", None),
)


class EnvironmentError(RuntimeError):
    """The soak environment could not be built on this host."""


@dataclass
class SoakEnvironment:
    """Describe one prepared environment and its handles."""

    workdir: Path
    home: Path
    run_dir: Path
    certs: Path
    logs: Path
    collection: str
    namespace: str = ""
    owner_id: str = "soak-owner"
    llm_key: str = ""
    cloud: bool = False
    llm_mode: str = "deterministic"
    untrusted_ca: Path | None = None
    tokens: dict = field(default_factory=dict)
    grants: list = field(default_factory=list)
    ports: dict = field(default_factory=dict)
    owner_endpoint: str = ""
    owner_internal: str = ""
    qdrant_endpoint: str = ""
    qdrant_admin_endpoint: str = ""
    policy_path: Path | None = None
    config_path: Path | None = None
    owner_log: Path | None = None
    ca_file: Path | None = None
    api_key: str = ""
    proxy: subprocess.Popen | None = None
    proxy_log: object | None = None
    containers: list = field(default_factory=list)
    container_names: list = field(default_factory=list)
    toxiproxy_api: str = ""
    lifecycle: bool = True

    def public(self):
        """Return an artifact-safe summary that never carries a credential."""
        return {
            "collection": self.collection,
            "ports": dict(self.ports),
            "owner_endpoint": self.owner_endpoint,
            "owner_internal": self.owner_internal,
            "qdrant_endpoint": self.qdrant_endpoint,
            "qdrant_admin_endpoint": self.qdrant_admin_endpoint,
            "lifecycle": self.lifecycle,
            "llm_mode": self.llm_mode,
            "owner_id": self.owner_id,
            "containers": list(self.containers),
            "nginx_pid": self.proxy.pid if self.proxy is not None else None,
        }


def require_tools(*names):
    """Fail early with an actionable message when a host tool is missing."""
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise EnvironmentError(
            "Missing required host tool(s): " + ", ".join(missing) + ". "
            "Install them before running the soak harness."
        )


def docker_available():
    """Return whether a reachable Docker daemon is available."""
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                timeout=30,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _run(command, *, cwd=None, env=None, check=True, timeout=300):
    """Run one host command and return its completed process."""
    result = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
    )
    if check and result.returncode != 0:
        raise EnvironmentError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
        )
    return result


def _openssl(*args):
    """Run one openssl invocation with captured output."""
    return _run(["openssl", *args], timeout=120)


def issue_certificates(certs):
    """Create a private CA and the two leaf certificates the run needs."""
    certs.mkdir(parents=True, exist_ok=True, mode=0o700)
    ca_key, ca_file = certs / "ca.key", certs / "ca.pem"
    if not ca_file.exists():
        _openssl(
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-subj",
            "/CN=qdrant-memory-soak-ca",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-addext",
            "subjectKeyIdentifier=hash",
            "-addext",
            "authorityKeyIdentifier=keyid:always",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_file),
        )
    extensions = certs / "leaf.ext"
    extensions.write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid,issuer\n"
        "subjectAltName=IP:127.0.0.1,DNS:localhost\n"
    )
    for name in ("mcp", "qdrant"):
        certificate = certs / f"{name}.pem"
        key = certs / f"{name}.key"
        request = certs / f"{name}.csr"
        if certificate.exists() and key.exists():
            continue
        _openssl(
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(request),
        )
        _openssl(
            "x509",
            "-req",
            "-in",
            str(request),
            "-CA",
            str(ca_file),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-days",
            "2",
            "-extfile",
            str(extensions),
            "-out",
            str(certificate),
        )
    untrusted = certs / "untrusted-ca.pem"
    if not untrusted.exists():
        _openssl(
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-subj",
            "/CN=soak-untrusted-ca",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-addext",
            "subjectKeyIdentifier=hash",
            "-keyout",
            str(certs / "untrusted-ca.key"),
            "-out",
            str(untrusted),
        )
    for path in certs.iterdir():
        if path.suffix in {".key", ".pem", ".csr", ".srl", ".ext"}:
            os.chmod(path, 0o600)
    return {"ca": ca_file, "untrusted_ca": untrusted}


def nginx_config(env, *, mcp_port, qdrant_port, upstream_mcp, upstream_qdrant):
    """Render the TLS, header and buffering policy the deployment documents."""
    certs = env.certs
    prefix = env.workdir / "nginx"
    prefix.mkdir(parents=True, exist_ok=True)
    body = f"""worker_processes 1;
pid {prefix}/nginx.pid;
error_log {prefix}/error.log warn;
daemon off;
events {{ worker_connections 256; }}
http {{
    access_log off;
    client_body_temp_path {prefix}/body;
    proxy_temp_path {prefix}/proxy;
    fastcgi_temp_path {prefix}/fastcgi;
    uwsgi_temp_path {prefix}/uwsgi;
    scgi_temp_path {prefix}/scgi;
    client_body_buffer_size 128k;
    server_tokens off;

    server {{
        listen 127.0.0.1:{mcp_port} ssl;
        server_name soak-owner;
        ssl_certificate {certs / "mcp.pem"};
        ssl_certificate_key {certs / "mcp.key"};
        ssl_protocols TLSv1.2 TLSv1.3;
        client_max_body_size 128k;
        client_body_timeout 10s;
        client_header_timeout 10s;
        location ~ ^/(mcp|events|events/manage)$ {{
            if ($http_forwarded != '') {{ return 403; }}
            if ($http_x_forwarded_host != '') {{ return 403; }}
            proxy_set_header Host $http_host;
            proxy_set_header Forwarded '';
            proxy_set_header X-Forwarded-For '';
            proxy_set_header X-Forwarded-Host '';
            proxy_set_header X-Forwarded-Proto '';
            proxy_pass http://127.0.0.1:{upstream_mcp};
            proxy_buffering off;
            proxy_read_timeout 120s;
            proxy_http_version 1.1;
            proxy_set_header Connection '';
        }}
    }}

    server {{
        listen 127.0.0.1:{qdrant_port} ssl;
        server_name soak-qdrant;
        ssl_certificate {certs / "qdrant.pem"};
        ssl_certificate_key {certs / "qdrant.key"};
        ssl_protocols TLSv1.2 TLSv1.3;
        client_max_body_size 32m;
        client_body_timeout 30s;
        location / {{
            proxy_set_header Host $http_host;
            proxy_pass http://127.0.0.1:{upstream_qdrant};
            proxy_http_version 1.1;
            proxy_set_header Connection '';
            proxy_read_timeout 120s;
        }}
    }}
}}
"""
    path = env.workdir / "nginx.conf"
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def start_nginx(env, config_path):
    """Start the host nginx as a supervised child process."""
    env.proxy_log = open(env.logs / "nginx.log", "a", encoding="utf-8")
    env.proxy = subprocess.Popen(
        ["nginx", "-c", str(config_path), "-p", str(env.workdir / "nginx")],
        stdout=env.proxy_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return env.proxy


def wait_for_port(port, *, timeout=30.0, host="127.0.0.1"):
    """Wait until a TCP listener accepts a connection."""
    import socket

    def probe():
        with socket.create_connection((host, port), timeout=1):
            return True

    return wait_until(probe, timeout=timeout, interval=0.2)


def start_container(name, image, ports, *, environment=None, extra=(), arguments=()):
    """Start one detached container and return its identifier."""
    _run(["docker", "rm", "-f", name], check=False, timeout=60)
    command = ["docker", "run", "-d", "--name", name, *extra]
    for host_port, container_port in ports.items():
        command += ["-p", f"127.0.0.1:{host_port}:{container_port}"]
    for key, value in (environment or {}).items():
        command += ["-e", f"{key}={value}"]
    command.append(image)
    command += list(arguments)
    result = _run(command, timeout=300)
    return result.stdout.strip()


def wait_for_qdrant(port, api_key, *, timeout=90.0):
    """Wait until Qdrant answers an authenticated version request."""
    import httpx

    def probe():
        response = httpx.get(
            f"http://127.0.0.1:{port}/",
            headers={"api-key": api_key},
            timeout=3,
            trust_env=False,
        )
        return response.status_code == 200

    return wait_until(probe, timeout=timeout, interval=0.3)


def host_networking():
    """Return whether the host Docker daemon shares the runner network."""
    return sys.platform.startswith("linux")


class Toxic:
    """Manage the optional toxiproxy instance used for network faults."""

    def __init__(self, api_port, upstream_host):
        """Bind the control API port and the upstream address family."""
        self.api = f"http://127.0.0.1:{api_port}"
        self.upstream_host = upstream_host

    def create(self, name, listen_port, upstream_port):
        """Create one TCP proxy from a loopback listener to a host listener."""
        import httpx

        listen = (
            f"127.0.0.1:{listen_port}"
            if self.upstream_host == "127.0.0.1"
            else f"0.0.0.0:{listen_port}"
        )
        response = httpx.post(
            f"{self.api}/proxies",
            json={
                "name": name,
                "listen": listen,
                "upstream": f"{self.upstream_host}:{upstream_port}",
                "enabled": True,
            },
            timeout=10,
            trust_env=False,
        )
        response.raise_for_status()
        return response.json()

    def add(self, name, toxic, attributes, *, toxicity=1.0):
        """Attach one toxic to a proxy."""
        import httpx

        response = httpx.post(
            f"{self.api}/proxies/{name}/toxics",
            json={"name": toxic, "type": toxic, "attributes": attributes, "toxicity": toxicity},
            timeout=10,
            trust_env=False,
        )
        response.raise_for_status()
        return response.json()

    def list_toxics(self, name):
        """Return the toxics currently attached to one proxy."""
        import httpx

        response = httpx.get(f"{self.api}/proxies/{name}/toxics", timeout=10, trust_env=False)
        response.raise_for_status()
        return response.json()

    def clear(self, name):
        """Remove every toxic from a proxy, one explicit name at a time."""
        import httpx

        removed = []
        for toxic in self.list_toxics(name):
            toxic_name = toxic.get("name")
            if not toxic_name:
                continue
            response = httpx.delete(
                f"{self.api}/proxies/{name}/toxics/{toxic_name}", timeout=10, trust_env=False
            )
            if response.status_code not in (204, 404):
                response.raise_for_status()
            removed.append(toxic_name)
        return removed

    def wait_ready(self, timeout=60.0):
        """Wait until the control API answers."""
        import httpx

        def probe():
            return httpx.get(f"{self.api}/version", timeout=3, trust_env=False).status_code == 200

        return wait_until(probe, timeout=timeout, interval=0.3)


def build_grants(owner_id="soak-owner"):
    """Build credential grants; the raw token never enters the policy file.

    ``owner_id`` scopes every shared record in the profile's space, so a fixed
    cloud collection can be cleaned up one run at a time without touching
    another run's data or deleting the collection itself.
    """
    import hashlib
    import secrets

    grants, tokens = [], {}
    for principal, logical, device, space, visibility, permission, rule in GRANT_SPECS:
        token = secrets.token_urlsafe(36)
        tokens[principal] = token
        grant = {
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "principal": principal,
            "owner_id": owner_id,
            "logical_agent_id": logical,
            "device_id": device,
            "space_id": space,
            "visibility": visibility,
            "permission": permission,
        }
        if rule:
            grant["capture_rule_id"] = rule
        grants.append(grant)
    return grants, tokens


def destination_tuple(qdrant_endpoint):
    """Return the protected destination tuple the policy must bind exactly."""
    from qdrant_memory.config import backend_destination

    return list(backend_destination({"mode": "server", "url": qdrant_endpoint, "path": None}))


def write_profile(env, *, qdrant_endpoint, provider_base_url, api_key, lifecycle=True):
    """Write the private Owner profile and its protected destination policy."""
    from qdrant_memory.config import embedding_config, fingerprint
    from qdrant_memory.models import digest
    from qdrant_memory.standalone import load_standalone_config

    env.home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(env.home, 0o700)
    profile = {
        "scope": {"user_id": "soak-owner", "agent_id": None},
        "qdrant": {
            "mode": "server",
            "url": qdrant_endpoint,
            "api_key": api_key,
            "collection": env.collection,
            "timeout_seconds": 10,
        },
        "embedding": {
            "mode": "plugin",
            "provider": "openai-compatible",
            "model": "soak-embedding-v1",
            "base_url": provider_base_url,
            "dimensions": 96,
            "distance": "Cosine",
            "batch_size": 32,
            "timeout_seconds": 30,
        },
        "llm": {
            "mode": "override",
            "provider": "openai-compatible",
            "model": "soak-extractor-v1",
            "base_url": provider_base_url,
            "max_output_tokens": 2048,
            "response_format_mode": "json_schema",
            "api_key_env": "SOAK_OWNER_LLM_KEY",
        },
        "search": {"top_k": 8, "candidate_k": 24},
        "write": {
            "batch_size": 16,
            "max_attempts": 3,
            "backoff_base_seconds": 0.05,
            "backoff_max_seconds": 0.5,
            "shutdown_timeout_seconds": 5,
        },
    }
    env.config_path = env.home / "qdrant-memory.json"
    # The Owner profile is not an artifact; it must keep the literal credential.
    write_json(env.config_path, profile, scrub=False)

    cfg = load_standalone_config(env.home)
    from qdrant_memory.config import ledger_namespace

    env.namespace = ledger_namespace(cfg)
    binding = {
        "backend": "server",
        "destination": destination_tuple(qdrant_endpoint),
        "collection": env.collection,
        "embedding_fingerprint": fingerprint(embedding_config(cfg)),
        "owner_profile": digest(str(env.home.resolve())),
        "schema_version": 1,
        "security_mode": "protected-server",
    }
    grants, tokens = build_grants(env.owner_id)
    env.grants, env.tokens = grants, tokens
    env.policy_path = env.home / "policy.json"
    write_json(env.policy_path, {"clients": grants, "destination": binding}, scrub=False)
    env.lifecycle = lifecycle
    return cfg


def initialize_collection(env, *, cwd, existing=False, cloud=False):
    """Create the collection and its ledger through the product CLI."""
    environment = dict(os.environ)
    # Only the loopback listeners use the private CA; a cloud endpoint must be
    # validated against the system trust store.
    if cloud:
        environment.pop("SSL_CERT_FILE", None)
    else:
        environment["SSL_CERT_FILE"] = str(env.ca_file)
    command = [sys.executable, "-m", "qdrant_memory.standalone", "--home", str(env.home), "init"]
    if existing:
        # An existing shared test collection is reused only after validation.
        command += ["--existing", "use"]
    result = _run(
        command,
        cwd=str(cwd),
        env=environment,
        check=False,
        timeout=300,
    )
    if result.returncode != 0:
        raise EnvironmentError(
            "Collection initialization failed: " + (result.stdout + result.stderr)[-2000:]
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


def build_environment(
    *, workdir, collection, provider_base_url, scene, seed, lifecycle=True, owner_id=None
):
    """Prepare every moving part for one soak window.

    Returns a populated :class:`SoakEnvironment`. The caller owns shutdown via
    :func:`teardown`.
    """
    require_tools("openssl", "nginx", "docker")
    if not docker_available():
        raise EnvironmentError("The Docker daemon is not reachable")
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(workdir, 0o700)
    env = SoakEnvironment(
        workdir=workdir,
        home=workdir / "home",
        run_dir=(workdir / "run").resolve(),
        certs=workdir / "certs",
        logs=workdir / "logs",
        collection=collection,
        lifecycle=lifecycle,
        owner_id=owner_id or "soak-owner-" + f"{seed & 0xFFFF:04x}",
    )
    for path in (env.home, env.logs, env.run_dir):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    try:
        return _build_local_services(
            env,
            provider_base_url=provider_base_url,
            scene=scene,
            seed=seed,
            lifecycle=lifecycle,
        )
    except BaseException:
        # A half-built environment must not leave containers or a proxy behind.
        with contextlib.suppress(Exception):
            teardown(env)
        raise


def _build_local_services(env, *, provider_base_url, scene, seed, lifecycle):
    """Start the local services for an already-created work directory."""
    certificates = issue_certificates(env.certs)
    env.ca_file = certificates["ca"]
    env.untrusted_ca = certificates["untrusted_ca"]

    qdrant_port = free_port()
    qdrant_api_key = os.urandom(24).hex()
    SECRETS.register(qdrant_api_key)
    container = start_container(
        f"soak-qdrant-{seed}",
        QDRANT_IMAGE,
        {qdrant_port: 6333},
        environment={"QDRANT__SERVICE__API_KEY": qdrant_api_key},
    )
    env.containers.append(container)
    env.container_names.append(f"soak-qdrant-{seed}")
    if not wait_for_qdrant(qdrant_port, qdrant_api_key):
        raise EnvironmentError("Qdrant did not become ready")
    env.ports["qdrant"] = qdrant_port

    mcp_port = free_port()
    qdrant_tls_port = free_port()
    owner_plain_port = free_port()
    owner_local_port = free_port()
    config_path = nginx_config(
        env,
        mcp_port=mcp_port,
        qdrant_port=qdrant_tls_port,
        upstream_mcp=owner_plain_port,
        upstream_qdrant=qdrant_port,
    )
    start_nginx(env, config_path)
    if not (wait_for_port(mcp_port) and wait_for_port(qdrant_tls_port)):
        raise EnvironmentError("nginx did not bind both TLS listeners")
    env.ports.update(
        mcp=mcp_port,
        qdrant_tls=qdrant_tls_port,
        owner_plain=owner_plain_port,
        owner_local=owner_local_port,
    )

    qdrant_endpoint = f"https://127.0.0.1:{qdrant_tls_port}"
    owner_endpoint = f"https://127.0.0.1:{mcp_port}/mcp"
    env.qdrant_admin_endpoint = qdrant_endpoint

    if scene == "network":
        api_port = free_port()
        qdrant_proxy_port = free_port()
        client_proxy_port = free_port()
        if host_networking():
            # The container shares the runner network, so every listener and
            # upstream stays on loopback and nothing is published off-host.
            toxiproxy = start_container(
                f"soak-toxiproxy-{seed}",
                TOXIPROXY_IMAGE,
                {},
                extra=("--network", "host"),
                arguments=("-host=127.0.0.1", f"-port={api_port}"),
            )
            upstream_host = "127.0.0.1"
        else:
            toxiproxy = start_container(
                f"soak-toxiproxy-{seed}",
                TOXIPROXY_IMAGE,
                {
                    api_port: 8474,
                    qdrant_proxy_port: qdrant_proxy_port,
                    client_proxy_port: client_proxy_port,
                },
            )
            upstream_host = "host.docker.internal"
        env.containers.append(toxiproxy)
        env.container_names.append(f"soak-toxiproxy-{seed}")
        toxic = Toxic(api_port, upstream_host)
        if not toxic.wait_ready():
            raise EnvironmentError("Toxiproxy control API did not become ready")
        env.toxiproxy_api = toxic.api
        toxic.create(QDRANT_PROXY, qdrant_proxy_port, qdrant_tls_port)
        toxic.create(CLIENT_PROXY, client_proxy_port, mcp_port)
        env.ports.update(
            toxiproxy_api=api_port,
            qdrant_proxy=qdrant_proxy_port,
            client_proxy=client_proxy_port,
        )
        qdrant_endpoint = f"https://127.0.0.1:{qdrant_proxy_port}"
        owner_endpoint = f"https://127.0.0.1:{client_proxy_port}/mcp"

    env.qdrant_endpoint = qdrant_endpoint
    env.owner_endpoint = owner_endpoint
    env.owner_internal = f"https://127.0.0.1:{mcp_port}/mcp"
    env.owner_log = env.logs / "owner.log"
    write_profile(
        env,
        qdrant_endpoint=qdrant_endpoint,
        provider_base_url=provider_base_url,
        api_key=qdrant_api_key,
        lifecycle=lifecycle,
    )
    env.api_key = qdrant_api_key
    SECRETS.register(qdrant_api_key)
    for token in env.tokens.values():
        SECRETS.register(token)
    return env


def build_cloud_environment(
    *,
    workdir,
    collection,
    provider_base_url,
    scene,
    seed,
    owner_id,
    cloud_url,
    cloud_api_key,
    lifecycle=True,
    llm=None,
):
    """Prepare a cloud-backed window: local Owner and proxy, remote Qdrant.

    The cloud job reuses the same Owner, gateway and harness as the local run.
    Only the Qdrant backend changes. The collection is the fixed test collection
    the operator provisioned; every record this run writes carries a run-scoped
    ``owner_id`` so cleanup can remove exactly those records and nothing else.
    """
    import re

    require_tools("openssl", "nginx")
    if scene == "network":
        raise EnvironmentError("The cloud backend has no local Toxiproxy topology")
    if not isinstance(cloud_url, str) or not cloud_url.startswith("https://"):
        raise EnvironmentError("SOAK_QDRANT_URL must be an https:// endpoint")
    if not cloud_api_key:
        raise EnvironmentError("SOAK_QDRANT_API_KEY is required for the cloud backend")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}", collection or ""):
        raise EnvironmentError("SOAK_QDRANT_COLLECTION is not a valid collection name")
    if not re.fullmatch(r"[a-z0-9-]{1,60}", owner_id or ""):
        raise EnvironmentError("The run owner identity is not a safe scope value")
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(workdir, 0o700)
    env = SoakEnvironment(
        workdir=workdir,
        home=workdir / "home",
        run_dir=(workdir / "run").resolve(),
        certs=workdir / "certs",
        logs=workdir / "logs",
        collection=collection,
        lifecycle=lifecycle,
        owner_id=owner_id,
        cloud=True,
    )
    for path in (env.home, env.logs, env.run_dir):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    try:
        return _build_cloud_services(
            env,
            provider_base_url=provider_base_url,
            lifecycle=lifecycle,
            llm=llm,
            cloud_url=cloud_url,
            cloud_api_key=cloud_api_key,
        )
    except BaseException:
        with contextlib.suppress(Exception):
            teardown(env)
        raise


def _build_cloud_services(env, *, provider_base_url, lifecycle, llm, cloud_url, cloud_api_key):
    """Start the Owner proxy and write the cloud profile."""
    certificates = issue_certificates(env.certs)
    env.ca_file = certificates["ca"]
    env.untrusted_ca = certificates["untrusted_ca"]
    mcp_port = free_port()
    owner_plain_port = free_port()
    owner_local_port = free_port()
    config_path = nginx_config(
        env,
        mcp_port=mcp_port,
        qdrant_port=free_port(),
        upstream_mcp=owner_plain_port,
        upstream_qdrant=free_port(),
    )
    start_nginx(env, config_path)
    if not wait_for_port(mcp_port):
        raise EnvironmentError("nginx did not bind the MCP TLS listener")
    env.ports.update(
        mcp=mcp_port,
        owner_plain=owner_plain_port,
        owner_local=owner_local_port,
    )
    env.qdrant_endpoint = cloud_url.rstrip("/")
    env.qdrant_admin_endpoint = env.qdrant_endpoint
    env.owner_endpoint = f"https://127.0.0.1:{mcp_port}/mcp"
    env.owner_internal = env.owner_endpoint
    env.owner_log = env.logs / "owner.log"
    env.api_key = cloud_api_key
    SECRETS.register(cloud_api_key)
    _write_cloud_profile(
        env,
        cloud_url=env.qdrant_endpoint,
        provider_base_url=provider_base_url,
        api_key=cloud_api_key,
        lifecycle=lifecycle,
        llm=llm,
    )
    return env


def _write_cloud_profile(env, *, cloud_url, provider_base_url, api_key, lifecycle, llm=None):
    """Write the cloud profile with a protected-cloud destination binding."""
    from qdrant_memory.config import backend_destination, embedding_config, fingerprint
    from qdrant_memory.models import digest
    from qdrant_memory.standalone import load_standalone_config

    profile = {
        "scope": {"user_id": "soak-owner", "agent_id": None},
        "qdrant": {
            "mode": "cloud",
            "url": cloud_url,
            "api_key": api_key,
            "collection": env.collection,
            "timeout_seconds": 30,
        },
        "embedding": {
            "mode": "plugin",
            "provider": "openai-compatible",
            "model": "soak-embedding-v1",
            "base_url": provider_base_url,
            "dimensions": 96,
            "distance": "Cosine",
            "batch_size": 32,
            "timeout_seconds": 30,
        },
        "llm": (
            {
                "mode": "override",
                "provider": "openai-compatible",
                "model": llm["model"],
                "base_url": llm["base_url"],
                "max_output_tokens": 2048,
                "response_format_mode": "json_schema",
                "api_key_env": "SOAK_OWNER_LLM_KEY",
            }
            if llm
            else {
                "mode": "override",
                "provider": "openai-compatible",
                "model": "soak-extractor-v1",
                "base_url": provider_base_url,
                "max_output_tokens": 2048,
                "response_format_mode": "json_schema",
                "api_key_env": "SOAK_OWNER_LLM_KEY",
            }
        ),
        "write": {
            "batch_size": 16,
            "max_attempts": 3,
            "backoff_base_seconds": 0.2,
            "backoff_max_seconds": 2,
            "shutdown_timeout_seconds": 5,
        },
    }
    env.config_path = env.home / "qdrant-memory.json"
    write_json(env.config_path, profile, scrub=False)
    cfg = load_standalone_config(env.home)
    from qdrant_memory.config import ledger_namespace

    env.namespace = ledger_namespace(cfg)
    binding = {
        "backend": "cloud",
        "destination": list(backend_destination(cfg["qdrant"])),
        "collection": env.collection,
        "embedding_fingerprint": fingerprint(embedding_config(cfg)),
        "owner_profile": digest(str(env.home.resolve())),
        "schema_version": 1,
        "security_mode": "protected-cloud",
    }
    grants, tokens = build_grants(env.owner_id)
    env.grants, env.tokens = grants, tokens
    env.policy_path = env.home / "policy.json"
    write_json(env.policy_path, {"clients": grants, "destination": binding}, scrub=False)
    env.lifecycle = lifecycle
    env.llm_key = (llm or {}).get("key", "")
    env.llm_mode = "real" if llm else "deterministic"
    return cfg


def delete_run_records(env):
    """Delete this run's records from the fixed test collection, never the collection."""
    import os
    import re

    from qdrant_client import QdrantClient, models

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}", env.collection or ""):
        raise EnvironmentError("Refusing to touch an unexpected collection name")
    if not re.fullmatch(r"[a-z0-9-]{1,60}", env.owner_id or "") or env.owner_id == "soak-owner":
        raise EnvironmentError("Refusing to delete records without a run-scoped owner identity")
    previous = os.environ.get("SSL_CERT_FILE")
    os.environ.pop("SSL_CERT_FILE", None)
    try:
        client = QdrantClient(url=env.qdrant_admin_endpoint, api_key=env.api_key, timeout=30)
        try:
            # The identity point and other runs keep their own user_id; the run
            # removes only the rows it wrote under its own scope.
            selector = models.Filter(
                must=[
                    models.FieldCondition(
                        key="user_id", match=models.MatchValue(value=env.owner_id)
                    )
                ]
            )
            before = client.count(env.collection, count_filter=selector, exact=True).count
            if before:
                client.delete(
                    env.collection,
                    points_selector=models.FilterSelector(filter=selector),
                    wait=True,
                )
            after = client.count(env.collection, count_filter=selector, exact=True).count
            return {
                "collection": env.collection,
                "owner_id": env.owner_id,
                "records_before": before,
                "records_after": after,
                "collection_retained": client.collection_exists(env.collection),
                "deleted": after == 0,
            }
        finally:
            client.close()
    finally:
        if previous is not None:
            os.environ["SSL_CERT_FILE"] = previous


def teardown(env):
    """Stop every service and release the destination claim for this run."""
    if env.proxy is not None and env.proxy.poll() is None:
        env.proxy.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            env.proxy.wait(timeout=15)
        if env.proxy.poll() is None:
            env.proxy.kill()
            env.proxy.wait(timeout=10)
    if env.proxy_log is not None:
        with contextlib.suppress(OSError):
            env.proxy_log.close()
    for container in env.containers:
        _run(["docker", "rm", "-f", container], check=False, timeout=120)
    env.containers = []
    # Confirm this run's test services really stopped; leftovers are reported.
    leftover = []
    for name in env.container_names:
        listing = _run(
            ["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", f"name={name}"],
            check=False,
            timeout=60,
        )
        if name in {line.strip() for line in listing.stdout.splitlines()}:
            leftover.append(name)
    # A claim outlives a profile directory, so the run removes its own claim.
    removed = []
    if env.config_path and env.config_path.exists():
        try:
            from qdrant_memory.shared_security import destination_key, registry_path
            from qdrant_memory.standalone import load_standalone_config

            key = destination_key(load_standalone_config(env.home))
            root = registry_path()
            for name in (key + ".json", key + ".lock"):
                path = root / name
                if path.exists() or path.is_symlink():
                    path.unlink()
                    removed.append(name)
        except Exception:  # noqa: BLE001 - cleanup must never mask a run result
            removed.append("unresolved")
    return {"removed_claims": removed, "leftover_containers": leftover}
