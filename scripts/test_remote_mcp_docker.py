"""Require real HTTPS MCP over two isolated container network namespaces."""

import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


def run(*args):
    """Capture operational output without publishing container logs or credentials."""
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def main():
    """Build local synthetic fixtures and remove only resources created by this run."""
    name = "qm-https-" + uuid.uuid4().hex[:10]
    network, server, client = name, name + "-owner", name + "-client"
    image = name + ":test"
    try:
        requirement = sys.argv[1] if len(sys.argv) > 1 else "mcp>=1.30,<2"
        run(
            "build",
            "-q",
            "-t",
            image,
            "--build-arg",
            "MCP_REQUIREMENT=" + requirement,
            "-f",
            "tests/remote/Dockerfile",
            ".",
        )
        run("network", "create", network)
        run(
            "run",
            "-d",
            "--name",
            server,
            "--network",
            network,
            "--network-alias",
            "owner",
            image,
            "server",
        )
        run("create", "--name", client, "--network", network, image, "client")
        with tempfile.TemporaryDirectory(prefix="qm-remote-ca-") as directory:
            certificate = str(Path(directory) / "owner-ca.pem")
            for _ in range(100):
                try:
                    run("cp", server + ":/tmp/tls/cert.pem", certificate)
                    break
                except subprocess.CalledProcessError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Synthetic TLS fixture failed startup")
            run("cp", certificate, client + ":/tmp/owner-ca.pem")
            print(run("start", "-a", client))
            exit_code = run("inspect", "--format", "{{.State.ExitCode}}", client)
            if exit_code != "0":
                raise RuntimeError("HTTPS MCP client failed; inspect isolated fixture locally")
            run("restart", server)
            run("rm", client)
            run("create", "--name", client, "--network", network, image, "recover")
            run("cp", certificate, client + ":/tmp/owner-ca.pem")
            print(run("start", "-a", client))
            if run("inspect", "--format", "{{.State.ExitCode}}", client) != "0":
                raise RuntimeError("HTTPS MCP restart/recovery client failed")
    finally:
        for container in (client, server):
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)
        subprocess.run(["docker", "image", "rm", image], capture_output=True)


if __name__ == "__main__":
    main()
