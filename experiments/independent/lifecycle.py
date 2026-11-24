"""Deterministic mutation and same-volume restart checks, never quality evidence."""

import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path

from server import CONFIG, IMAGE, PATHS, Server, fixture_vector

SCOPE = ["fixture-user", "fixture-agent"]


def check(server):
    """Assert lifecycle and scope invariants."""
    vector = fixture_vector("neworchid")
    assert "updated" not in server.search("oldmagnolia", vector, SCOPE, "sparse_fixture"), (
        "OLD_SPARSE_CONTENT"
    )
    assert "updated" in server.search("neworchid", vector, SCOPE, "sparse_fixture"), (
        "UPDATED_SPARSE_MISSING"
    )
    for path in PATHS:
        ids = server.search("deletecedar neworchid sharedtokens", vector, SCOPE, path)
        assert "deleted" not in ids, "DELETED_ID_LEAK"
        assert "updated" in ids, "UPDATED_ID_MISSING"
        assert not {"foreign-user", "foreign-agent", "null-agent"}.intersection(ids), (
            "CROSS_SCOPE_LEAK"
        )
        null_ids = server.search("sharedtokens", vector, ["fixture-user", None], path)
        assert null_ids == ["null-agent"], "NULL_AGENT_SCOPE_LEAK"


def main():
    """Execute the experiment and persist attributed results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:16333")
    parser.add_argument("--container", required=True, help="dedicated disposable bm25 container")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.container.startswith("bm25"):
        raise ValueError("restart allowed only for a dedicated bm25* container")
    result = dict(status="FAIL", evidence="deterministic fixture only", image=IMAGE, config=CONFIG)
    server = Server(args.server, "bm25_ind_lifecycle_" + uuid.uuid4().hex)

    def inspect():
        meta = json.loads(
            subprocess.check_output(["docker", "inspect", args.container], text=True)
        )[0]
        assert meta["Config"]["Image"] == IMAGE, "unpinned lifecycle image"
        mounts = [
            (m["Source"], m["Destination"])
            for m in meta["Mounts"]
            if m["Destination"] == "/qdrant/storage"
        ]
        assert mounts, "persistent storage mount required"
        return dict(container_id=meta["Id"], mounts=mounts, started=meta["State"]["StartedAt"])

    try:
        before = inspect()
        server.create(32)

        def insert(mid, text, scope=SCOPE):
            server.upsert(dict(id=mid, text=text, scope=scope), fixture_vector(text))

        insert("updated", "oldmagnolia sharedtokens")
        assert server.search(
            "oldmagnolia", fixture_vector("oldmagnolia"), SCOPE, "sparse_fixture"
        ) == ["updated"]
        for path in PATHS:
            assert server.search("oldmagnolia", fixture_vector("oldmagnolia"), SCOPE, path) == [
                "updated"
            ], "ADD_NOT_VISIBLE"
        insert("updated", "neworchid sharedtokens")
        insert("deleted", "deletecedar sharedtokens")
        for path in PATHS:
            assert "deleted" in server.search(
                "deletecedar", fixture_vector("deletecedar"), SCOPE, path
            ), "DELETE_PRECONDITION"
        insert("foreign-user", "sharedtokens neworchid", ["another-user", "fixture-agent"])
        insert("foreign-agent", "sharedtokens neworchid", ["fixture-user", "another-agent"])
        insert("null-agent", "sharedtokens neworchid", ["fixture-user", None])
        server.delete("deleted")
        check(server)
        subprocess.run(["docker", "restart", args.container], check=True, capture_output=True)
        for _ in range(60):
            try:
                if server.http.get("/readyz").status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(1)
        after = inspect()
        assert (
            before["mounts"] == after["mounts"] and before["container_id"] == after["container_id"]
        )
        assert before["started"] != after["started"], "container did not restart"
        check(server)
        result.update(
            status="PASS",
            before=before,
            after=after,
            assertions=[
                "add visible in both paths",
                "replace removes old sparse token",
                "replace indexes new sparse token",
                "delete absent in both paths",
                "foreign user isolated",
                "foreign agent isolated",
                "null agent isolated",
                "same container and volume restarted",
                "all checks repeated after restart",
            ],
        )
        server.request("DELETE")
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        server.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
