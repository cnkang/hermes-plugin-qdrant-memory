#!/usr/bin/env python3
"""Rehearse a self-hosted Qdrant patch upgrade on a disposable volume.

The rehearsal keeps one named volume across two immutable server images and
proves three things before an operator upgrades a real deployment:

1. the previous image writes points that the next image reads back unchanged;
2. a filesystem copy of the stopped volume restores into a fresh volume;
3. the running server reports the expected version on both sides.

It never touches a configured Hermes profile, a Cloud deployment or a running
Qdrant. Every container and volume it creates is prefixed with the run id and
removed at the end unless ``--keep`` is passed.

Usage::

    python3 scripts/qdrant_upgrade_rehearsal.py --workdir /tmp/qdrant-rehearsal
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

PREVIOUS_IMAGE = (
    "qdrant/qdrant@sha256:9bfc89f9914fc14876a88c05270685921a55b8877b254a98bcc1cc2f0a424cd2"
)
CURRENT_IMAGE = (
    "qdrant/qdrant@sha256:d18c8e5061be3eaea2f53fbee1f0bf0382dc59cbb9b7112e41ffcc937ec14220"
)
PREVIOUS_VERSION = "1.20.0"
CURRENT_VERSION = "1.20.1"
COLLECTION = "upgrade_rehearsal"
VECTOR_SIZE = 4


class RehearsalError(RuntimeError):
    """Report one failed rehearsal step with its command context."""


def run(*args, check=True):
    """Run a docker command and return its stripped standard output."""
    result = subprocess.run(list(args), capture_output=True, text=True, check=False)
    if check and result.returncode:
        raise RehearsalError(
            f"{' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def start(image, volume, name, port):
    """Start one server on a fixed volume and wait for its readiness endpoint."""
    run(
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "-p",
        f"127.0.0.1:{port}:6333",
        "-v",
        f"{volume}:/qdrant/storage",
        image,
    )
    wait_ready(port)
    return f"http://127.0.0.1:{port}"


def wait_ready(port, attempts=120):
    """Poll the health endpoint until the server answers or the budget ends."""
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{port}/healthz"
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    raise RehearsalError(f"server on port {port} did not become ready")


def free_port():
    """Reserve an ephemeral local port and release it for Docker to bind."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def client(url):
    """Create a REST client for the rehearsal server."""
    from qdrant_client import QdrantClient

    return QdrantClient(url=url, timeout=30)


def seed(url):
    """Create one collection with deterministic points through the public API."""
    from qdrant_client import models as m

    qdrant = client(url)
    try:
        qdrant.create_collection(
            COLLECTION,
            vectors_config={"dense": m.VectorParams(size=VECTOR_SIZE, distance=m.Distance.COSINE)},
        )
        qdrant.create_payload_index(COLLECTION, "user_id", field_schema="keyword", wait=True)
        points = [
            m.PointStruct(
                id=index + 1,
                vector={"dense": [float(index), 1.0, 0.0, 0.0]},
                payload={"user_id": f"user-{index % 2}", "text": f"memory {index}"},
            )
            for index in range(6)
        ]
        qdrant.upsert(COLLECTION, points=points, wait=True)
        return snapshot(qdrant)
    finally:
        qdrant.close()


def snapshot(qdrant):
    """Read back the points, payloads and index schema for comparison."""
    points, _ = qdrant.scroll(COLLECTION, limit=100, with_payload=True, with_vectors=True)
    info = qdrant.get_collection(COLLECTION)
    return {
        "count": len(points),
        "points": sorted(
            (
                {"id": point.id, "payload": point.payload, "vector": point.vector["dense"]}
                for point in points
            ),
            key=lambda item: item["id"],
        ),
        "indexes": sorted(info.payload_schema),
    }


def read(url):
    """Snapshot one running rehearsal server."""
    qdrant = client(url)
    try:
        return snapshot(qdrant)
    finally:
        qdrant.close()


def backup(name, target):
    """Stop one container and copy its volume to a host directory."""
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    run("docker", "stop", name, check=False)
    run("docker", "cp", f"{name}:/qdrant/storage/.", str(target))


def restore(backup_dir, volume, name, image, port):
    """Create a fresh volume, copy the backup into it and start the server."""
    run("docker", "volume", "create", volume)
    run(
        "docker",
        "create",
        "--name",
        name,
        "-p",
        f"127.0.0.1:{port}:6333",
        "-v",
        f"{volume}:/qdrant/storage",
        image,
    )
    run("docker", "cp", f"{backup_dir}/.", f"{name}:/qdrant/storage")
    run("docker", "start", name)
    wait_ready(port)
    return f"http://127.0.0.1:{port}"


def stop(name):
    """Stop and remove one rehearsal container, ignoring absent containers."""
    run("docker", "rm", "-f", name, check=False)


def rehearse(workdir, port_previous=None, port_current=None, keep=False):
    """Run the upgrade and restore rehearsal and return its evidence."""
    token = uuid.uuid4().hex[:10]
    volume = f"qdm-rehearsal-{token}"
    backup_volume = f"qdm-rehearsal-restore-{token}"
    previous_name = f"qdm-rehearsal-prev-{token}"
    current_name = f"qdm-rehearsal-next-{token}"
    restore_name = f"qdm-rehearsal-restore-{token}"
    backup_dir = Path(workdir) / "backup"
    report: dict[str, object] = {"run": token, "collection": COLLECTION}
    try:
        run("docker", "volume", "create", volume)
        url = start(PREVIOUS_IMAGE, volume, previous_name, port_previous or free_port())
        report["previous_version"] = version(url)
        report["before"] = seed(url)
        backup(previous_name, backup_dir)
        report["backup_bytes"] = sum(
            path.stat().st_size for path in backup_dir.rglob("*") if path.is_file()
        )
        url = start(CURRENT_IMAGE, volume, current_name, port_current or free_port())
        report["current_version"] = version(url)
        report["after"] = read(url)
        stop(current_name)
        restored = restore(backup_dir, backup_volume, restore_name, CURRENT_IMAGE, free_port())
        report["restored"] = read(restored)
        report["ok"] = (
            report["previous_version"] == PREVIOUS_VERSION
            and report["current_version"] == CURRENT_VERSION
            and report["before"] == report["after"] == report["restored"]
        )
        return report
    finally:
        stop(previous_name)
        stop(current_name)
        stop(restore_name)
        if not keep:
            run("docker", "volume", "rm", "-f", volume, backup_volume, check=False)
            shutil.rmtree(backup_dir, ignore_errors=True)


def version(url):
    """Return the running server version string."""
    qdrant = client(url)
    try:
        return str(qdrant.info().version)
    finally:
        qdrant.close()


def main(argv=None):
    """Run the rehearsal and print one JSON report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--port-previous", type=int)
    parser.add_argument("--port-current", type=int)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    args.workdir.mkdir(parents=True, exist_ok=True)
    try:
        report = rehearse(args.workdir, args.port_previous, args.port_current, args.keep)
    except RehearsalError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
