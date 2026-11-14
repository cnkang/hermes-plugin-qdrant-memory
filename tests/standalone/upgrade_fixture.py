"""Two-process public-0.6.1 to built-0.6.3 storage acceptance helper.

Invoke with an installed Runtime interpreter in isolated mode, first with
``prepare HOME`` on 0.6.1, then ``verify HOME`` on 0.6.3. HOME must be disposable.
"""

import hashlib
import json
import os
import runpy
import sys
from importlib.metadata import version
from pathlib import Path

from qdrant_client import QdrantClient

from qdrant_memory.owner_lifecycle import OwnerLifecycle
from qdrant_memory.ownership import WriterLease
from qdrant_memory.shared_service import SharedOwner


def main():
    """Keep the same Qdrant data, ledger, profile and credential through upgrade."""
    phase, directory = sys.argv[1:]
    home = Path(directory).resolve()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(home)
    os.environ["XDG_DATA_HOME"] = str(home / "data")
    helpers = runpy.run_path(str(Path(__file__).resolve().parents[1] / "helpers.py"))
    cfg = helpers["config"](qdrant={"path": str(home / "qdrant")})
    token = "synthetic-upgrade-token-" * 3
    credential = {
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "principal": "synthetic",
        "owner_id": "synthetic-owner",
        "device_id": "upgrade",
        "logical_agent_id": "fixture",
        "space_id": "fixture-private",
        "visibility": "private",
        "permission": "write",
    }
    lease = WriterLease.for_config(home, cfg)
    rt = helpers["runtime"](home, client=QdrantClient(path=str(home / "qdrant")), cfg=cfg)
    try:
        owner = SharedOwner(rt, [credential])
        lifecycle = OwnerLifecycle(owner)
        event = {
            "event_id": "upgrade-turn",
            "session_id": "upgrade-session",
            "kind": "turn_completed",
            "user": "Synthetic Iris uses SQLite WAL.",
            "assistant": "ack",
        }
        if phase == "prepare":
            assert version("hermes-plugin-qdrant-memory") == "0.6.1"
            assert lifecycle.submit(token, event)["status"] == "COMMITTED"
            rows = owner.call(token, "qdrant_memory_search", {"query": "Iris"})["memories"]
            (home / "expected.json").write_text(json.dumps(rows))
            (home / "profile-sentinel").write_text("preserve this synthetic profile")
        elif phase == "verify":
            assert version("hermes-plugin-qdrant-memory") == "0.6.3"
            expected = json.loads((home / "expected.json").read_text())
            rows = owner.call(token, "qdrant_memory_search", {"query": "Iris"})["memories"]
            assert rows == expected
            assert lifecycle.inspect(token, "upgrade-turn")["status"] == "COMMITTED"
            assert lifecycle.retry(token, "upgrade-turn")["status"] == "COMMITTED"
            assert (home / "profile-sentinel").read_text() == "preserve this synthetic profile"
            assert rt.store.count() == 1
        else:
            raise ValueError("Expected prepare or verify")
        print(
            json.dumps(
                {
                    "phase": phase,
                    "version": version("hermes-plugin-qdrant-memory"),
                    "records": rt.store.count(),
                    "status": "PASS",
                }
            )
        )
    finally:
        rt.ledger.close()
        rt.store.close()
        lease.close()


if __name__ == "__main__":
    main()
