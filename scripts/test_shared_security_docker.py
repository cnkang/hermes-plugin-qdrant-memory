"""Run the disposable TLS/JWT isolation gate against a local Qdrant container."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


def token(secret, collection, access):
    """Sign short-lived collection grants without printing credentials."""

    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")

    header = encode(b'{"alg":"HS256","typ":"JWT"}')
    body = encode(
        json.dumps(
            {
                "exp": int(time.time()) + 900,
                "access": [{"collection": collection, "access": access}],
            }
        ).encode()
    )
    message = header + b"." + body
    return (
        message + b"." + encode(hmac.new(secret.encode(), message, hashlib.sha256).digest())
    ).decode()


def main():
    """Create only ephemeral container storage, credentials and fixture collections."""
    image = sys.argv[1] if len(sys.argv) > 1 else "qdrant/qdrant:v1.15.5"
    collection = "qm_security_" + secrets.token_hex(8)
    secret = secrets.token_urlsafe(48)
    container = None
    previous = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix="qdrant-isolation-") as directory:
            root = Path(directory).resolve()
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
                    "/CN=127.0.0.1",
                    "-addext",
                    "subjectAltName=IP:127.0.0.1",
                    "-keyout",
                    str(root / "key.pem"),
                    "-out",
                    str(root / "cert.pem"),
                ],
                check=True,
                capture_output=True,
            )
            env = {**os.environ, "QDRANT__SERVICE__API_KEY": secret}
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--detach",
                    "--pull=never",
                    "--publish",
                    "127.0.0.1::6333",
                    "--mount",
                    f"type=bind,src={root},dst=/tls,readonly",
                    "--env",
                    "QDRANT__SERVICE__API_KEY",
                    "--env",
                    "QDRANT__SERVICE__JWT_RBAC=true",
                    "--env",
                    "QDRANT__SERVICE__ENABLE_TLS=true",
                    "--env",
                    "QDRANT__TLS__CERT=/tls/cert.pem",
                    "--env",
                    "QDRANT__TLS__KEY=/tls/key.pem",
                    image,
                ],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            container = result.stdout.strip()
            port = (
                subprocess.check_output(["docker", "port", container, "6333/tcp"], text=True)
                .strip()
                .rsplit(":", 1)[1]
            )
            url = "https://127.0.0.1:" + port
            context = ssl.create_default_context(cafile=str(root / "cert.pem"))
            with httpx.Client(verify=context, trust_env=False) as client:
                for attempt in range(100):
                    try:
                        if (
                            client.get(url + "/readyz", headers={"api-key": secret}).status_code
                            == 200
                        ):
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Disposable Qdrant did not become ready")
            env.update(
                {
                    "SSL_CERT_FILE": str(root / "cert.pem"),
                    "QDRANT_MEMORY_SECURITY_LIVE": "1",
                    "QDRANT_MEMORY_SECURITY_URL": url,
                    "QDRANT_MEMORY_SECURITY_COLLECTION": collection,
                    "QDRANT_MEMORY_SECURITY_BOOTSTRAP_KEY": secret,
                    "QDRANT_MEMORY_SECURITY_OWNER_KEY": token(secret, collection, "rw"),
                    "QDRANT_MEMORY_SECURITY_READ_KEY": token(secret, collection, "r"),
                    "QDRANT_MEMORY_SECURITY_UNRELATED_KEY": token(
                        secret, "unrelated_fixture", "rw"
                    ),
                }
            )
            return subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/security/test_shared_security_live.py",
                    "-q",
                    "--tb=short",
                ],
                env=env,
                cwd=Path(__file__).resolve().parents[1],
            ).returncode
    finally:
        if container:
            subprocess.run(["docker", "rm", "--force", container], check=False, capture_output=True)
        os.umask(previous)


if __name__ == "__main__":
    raise SystemExit(main())
