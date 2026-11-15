"""Published MCP/Runtime to candidate wheel operator-upgrade acceptance.

Invoke with an installed interpreter in isolated mode:

    python -I tests/standalone/public_upgrade_acceptance.py prepare HOME 0.6.2
    python -I tests/standalone/public_upgrade_acceptance.py verify HOME 0.7.1

``prepare`` runs the published MCP 0.3.3 environment, which pins Runtime 0.6.2.
``verify`` runs a separate environment that installs only the candidate wheel.
Both phases share one disposable profile directory, never one virtualenv.
"""

import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path

from qdrant_client import QdrantClient

COLLECTION = "hermes_qdrant_memory"
SCOPE = {"user_id": "acceptance-user", "agent_id": "acceptance-agent"}
TEXTS = {
    "alpha": "alpha acceptance preference",
    "beta": "beta acceptance preference",
    "gamma": "gamma acceptance preference",
}
DELETED = "beta"


def _vector(text):
    """Return a deterministic non-zero three-dimensional embedding."""
    lower = text.lower()
    if "alpha" in lower:
        return [1.0, 0.0, 0.0]
    if "beta" in lower:
        return [0.0, 1.0, 0.0]
    if "gamma" in lower:
        return [0.0, 0.0, 1.0]
    if "delta" in lower:
        return [0.5, 0.5, 0.0]
    return [0.2, 0.3, 0.4]


class _EmbeddingHandler(BaseHTTPRequestHandler):
    """Serve the OpenAI-compatible embeddings contract used by both Runtimes."""

    def do_POST(self):
        """Answer one embeddings request without external services."""
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        texts = body.get("input") or []
        if isinstance(texts, str):
            texts = [texts]
        payload = {
            "data": [
                {"index": index, "embedding": _vector(text)} for index, text in enumerate(texts)
            ]
        }
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format, *args):
        """Keep the acceptance output JSON-only."""


def _free_port():
    """Reserve one loopback port for the duration of the acceptance."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _config(home):
    """Read the shared profile configuration."""
    return json.loads((home / "qdrant-memory.json").read_text())


def _write_config(home):
    """Publish the fixture profile before the first artifact writes data."""
    port = _free_port()
    config = {
        "schema_version": 1,
        "qdrant": {
            "mode": "embedded",
            "path": str(home / "qdrant"),
            "collection": COLLECTION,
        },
        "embedding": {
            "mode": "plugin",
            "provider": "openai-compatible",
            "model": "acceptance-embedding",
            "base_url": f"http://127.0.0.1:{port}/v1",
            "dimensions": 3,
            "distance": "Cosine",
            "batch_size": 8,
            "send_dimensions": False,
            "timeout_seconds": 10,
        },
        "scope": dict(SCOPE),
    }
    (home / "qdrant-memory.json").write_text(json.dumps(config, indent=2))


def _serve(home):
    """Start the fixture embedding service on the configured loopback port."""
    base_url = _config(home)["embedding"]["base_url"]
    port = int(base_url.rsplit(":", 1)[1].split("/", 1)[0])
    server = ThreadingHTTPServer(("127.0.0.1", port), _EmbeddingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _run(home, *arguments, expect=0):
    """Run one CLI process from the installed artifact and parse its JSON result."""
    environment = {"HOME": str(home), "XDG_DATA_HOME": str(home / "data")}
    command = [
        sys.executable,
        "-I",
        "-m",
        "qdrant_memory.standalone",
        "--home",
        str(home),
        *arguments,
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if completed.returncode != expect:
        raise AssertionError(
            f"{' '.join(arguments)} exited {completed.returncode}: {completed.stderr.strip()}"
        )
    return json.loads(completed.stdout) if completed.stdout.strip() else {}


def _records(home):
    """Return the inventory keyed by memory text."""
    result = _run(home, "list", "--limit", "50")
    return {item["text"]: item for item in result["memories"]}


def _prepare(home, expected):
    """Write sanitized data with the published MCP 0.3.3 / Runtime 0.6.2 artifact."""
    assert version("hermes-plugin-qdrant-memory") == expected, version(
        "hermes-plugin-qdrant-memory"
    )
    home.mkdir(parents=True, exist_ok=True)
    _write_config(home)
    server = _serve(home)
    try:
        _run(home, "init")
        for text in TEXTS.values():
            added = _run(home, "add", "--text", text)
            assert added["action"] == "ADD", added
        records = _records(home)
        assert set(records) == set(TEXTS.values()), records
        deleted = _run(home, "delete", "--id", records[TEXTS[DELETED]]["point_id"])
        assert deleted["action"] == "DELETE", deleted
        remaining = _records(home)
        assert TEXTS[DELETED] not in remaining, remaining
        doctor = _run(home, "doctor")
        fixture = {
            "published_version": version("hermes-plugin-qdrant-memory"),
            "remaining": {
                text: {
                    key: record[key]
                    for key in (
                        "point_id",
                        "user_id",
                        "agent_id",
                        "text",
                        "content_hash",
                        "created_at",
                    )
                }
                for text, record in remaining.items()
            },
            "deleted": {
                "text": TEXTS[DELETED],
                "point_id": records[TEXTS[DELETED]]["point_id"],
            },
            "embedding_fingerprint": doctor["embedding_fingerprint"],
            "scope": dict(SCOPE),
        }
        (home / "published-fixture.json").write_text(json.dumps(fixture, indent=2))
        print(
            json.dumps(
                {
                    "phase": "prepare",
                    "version": fixture["published_version"],
                    "records": len(remaining),
                    "deleted": fixture["deleted"]["point_id"],
                    "status": "PASS",
                }
            )
        )
    finally:
        server.shutdown()


def _verify(home, expected):
    """Apply the candidate wheel, restart the CLI and prove stored behavior."""
    assert version("hermes-plugin-qdrant-memory") == expected, version(
        "hermes-plugin-qdrant-memory"
    )
    fixture = json.loads((home / "published-fixture.json").read_text())
    server = _serve(home)
    try:
        planned = _run(home, "upgrade", "plan")
        assert planned["ok"] is True, planned
        assert planned["blockers"] == [], planned
        assert planned["checkpoint"]["points"] == len(fixture["remaining"]), planned
        plan_id = planned["plan_id"]
        applied = _run(home, "upgrade", "apply", "--plan", plan_id)
        assert applied["phase"] == "ready", applied
        assert applied["steps"]["backup"]["verified"] is True, applied
        assert applied["steps"]["activate"]["generation"] == 1, applied
        repeated = _run(home, "upgrade", "apply", "--plan", plan_id)
        assert repeated["already_applied"] is True, repeated

        status = _run(home, "upgrade", "status")
        assert status["phase"] == "ready", status
        assert status["plan_id"] == plan_id, status
        assert status["plan_stale"] is False, status
        backup_path = status["backup_path"]
        assert backup_path, status

        records = _records(home)
        assert set(records) == set(fixture["remaining"]), records
        for text, saved in fixture["remaining"].items():
            assert saved == {key: records[text].get(key) for key in saved}, (
                records[text],
                saved,
            )
        assert fixture["deleted"]["point_id"] not in {
            record["point_id"] for record in records.values()
        }, records
        assert fixture["deleted"]["text"] not in records, records

        doctor = _run(home, "doctor")
        assert doctor["embedding_fingerprint"] == fixture["embedding_fingerprint"], doctor
        assert doctor["policy"] == "accepted", doctor

        added = _run(home, "add", "--text", "delta acceptance preference")
        assert added["action"] == "ADD", added
        recalled = _run(home, "search", "--query", "delta acceptance preference")
        assert any(
            item.get("text") == "delta acceptance preference" for item in recalled["memories"]
        ), recalled

        backup = QdrantClient(path=str(home / backup_path / "qdrant"))
        try:
            points, _ = backup.scroll(COLLECTION, limit=100, with_payload=True)
            backup_ids = {str(point.id) for point in points}
        finally:
            backup.close()
        assert fixture["deleted"]["point_id"] not in backup_ids, backup_ids
        assert {saved["point_id"] for saved in fixture["remaining"].values()} <= backup_ids

        print(
            json.dumps(
                {
                    "phase": "verify",
                    "version": version("hermes-plugin-qdrant-memory"),
                    "plan_id": plan_id,
                    "generation": 1,
                    "backup": backup_path,
                    "records": len(records) + 1,
                    "fingerprint": doctor["embedding_fingerprint"],
                    "deleted_absent": True,
                    "new_write": added["id"],
                    "status": "PASS",
                }
            )
        )
    finally:
        server.shutdown()


def main():
    """Dispatch one acceptance phase."""
    if len(sys.argv) != 4:
        raise SystemExit("usage: public_upgrade_acceptance.py prepare|verify HOME VERSION")
    phase, directory, expected = sys.argv[1:4]
    home = Path(directory).resolve()
    if phase == "prepare":
        _prepare(home, expected)
    elif phase == "verify":
        _verify(home, expected)
    else:
        raise SystemExit("phase must be prepare or verify")


if __name__ == "__main__":
    main()
