"""Read-only Mem0 sources; source-ID-only target mapping and resumable manifests."""
from copy import deepcopy
from pathlib import Path
import json
import uuid
from urllib.parse import urlunsplit
from .version import VERSION
from .config import secret, validate_url
from .models import Scope, content_hash, digest, enforce_limits, now, payload, point_id
from .qdrant_store import build_client
from .retry import safe_error

EPOCH = "1970-01-01T00:00:00Z"
KNOWN = {"id", "memory", "text", "data", "user_id", "agent_id", "app_id", "run_id", "metadata",
         "categories", "hash", "created_at", "updated_at", "structured_attributes", "score",
         "_cloud_memory_id", "_cloud_created_at", "_cloud_updated_at"}


def read_json_records(path):
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        for key in ("memories", "results", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            if all(isinstance(v, dict) for v in data.values()):
                data = [{"id": key, **value} for key, value in data.items()]
    if not isinstance(data, list) or not all(isinstance(v, dict) for v in data):
        raise ValueError("Mem0 JSON must contain an array of memory objects")
    return data


def map_record(record, default_scope):
    identifier = record.get("_cloud_memory_id") or record.get("id")
    if not isinstance(identifier, (str, int)) or not str(identifier):
        raise ValueError("Mem0 record requires a stable source ID")
    text = record.get("memory") or record.get("text") or record.get("data")
    scope = Scope(record.get("user_id") or default_scope.user_id, record.get("agent_id", default_scope.agent_id))
    if not isinstance(scope.user_id, str) or (scope.agent_id is not None and not isinstance(scope.agent_id, str)):
        raise ValueError("Invalid source scope")
    origin = {"provider": "mem0", "id": str(identifier), "app_id": record.get("app_id"),
              "run_id": record.get("run_id"), "hash": record.get("hash"),
              "created_at": record.get("_cloud_created_at", record.get("created_at")),
              "updated_at": record.get("_cloud_updated_at", record.get("updated_at"))}
    metadata = {"legacy_mem0": record.get("metadata") or {},
                "legacy_mem0_extra": {k: v for k, v in record.items() if k not in KNOWN}}
    if "structured_attributes" in record:
        metadata["mem0_structured_attributes"] = record["structured_attributes"]
    value = payload(text, scope, "mem0_migration", record.get("run_id"),
                    category=record.get("categories") or [], metadata=metadata, cloud_origin=origin,
                    created_at=record.get("created_at") or EPOCH, updated_at=record.get("updated_at") or EPOCH)
    return point_id(scope, "mem0_migration", "mem0:" + str(identifier)), value


def source_client(cfg, home, source_config=None):
    """Legacy config is consulted exclusively during migration source reads."""
    path = Path(source_config) if source_config else Path(home) / "mem0.json"
    legacy = json.loads(path.read_text()) if path.exists() else {}
    block = legacy.get("vector_store", {}).get("config", {})
    if not block:
        block = legacy.get("config", {}).get("vector_store", {}).get("config", {})
    q = deepcopy(cfg["qdrant"])
    if block:
        q["api_key"] = block.get("api_key") or secret("QDRANT_API_KEY")
        q["url"] = block.get("url") or secret("QDRANT_URL")
        if block.get("path") and not q["url"]:
            q.update(mode="embedded", path=str(Path(block["path"]).expanduser()))
        else:
            q["mode"] = "server"
            q["url"] = q["url"] or legacy_endpoint(block)
            validate_url(q["url"])
    return build_client({"qdrant": q})


def legacy_endpoint(block):
    host = block.get("host", "127.0.0.1")
    # Legacy remote host-only settings default to TLS; local Qdrant uses HTTP.
    scheme = "http" if host in {"127.0.0.1", "localhost", "::1"} else "https"
    authority = f"[{host}]" if ":" in host else host
    return urlunsplit((scheme, f"{authority}:{block.get('port', 6333)}", "", "", ""))


def read_qdrant_records(client, collection):
    offset = None
    while True:
        rows, offset = client.scroll(collection, offset=offset, limit=256,
                                     with_payload=True, with_vectors=False)
        for row in rows:
            yield {"id": str(row.id), **(row.payload or {})}
        if offset is None:
            return


def plan(records, cfg):
    scope = Scope(**cfg["scope"])
    unique = {}
    for record in records:
        identifier, value = map_record(record, scope)
        value = enforce_limits(value, cfg["limits"])
        if identifier in unique and digest(unique[identifier]) != digest(value):
            # Export merges must resolve conflicts explicitly, never pick an
            # arbitrary record and silently lose a later update.
            raise ValueError("Conflicting duplicate Mem0 source ID")
        unique[identifier] = value
    return unique


def migrate(runtime, records, source_type, source_identifier, source_sha256=None,
            resume=False, retry_failed=False, verify=False):
    planned = durable_plan(runtime, records, source_type, source_identifier, source_sha256)
    manifest = resume_manifest(runtime, planned, source_type, source_identifier, source_sha256) if resume else None
    if manifest is None:
        manifest = create_manifest(runtime, planned, source_type, source_identifier, source_sha256)
    try:
        for record in manifest["records"].values():
            key = record["operation_key"]
            row = runtime.ledger.row("operations", key) if key else None
            if key and row is None:
                raise ValueError("Missing migration operation")
            # Only retry this migration's operations, never unrelated provider writes.
            if retry_failed and row and row["status"] == "FAILED":
                runtime.ledger.reset_operation(key)
        runtime.commit([r["operation_key"] for r in manifest["records"].values() if r["operation_key"]])
    finally:
        update_manifest_counts(runtime, manifest, len(planned))
    if verify and not verify_manifest(runtime.store, manifest)["ok"]:
        raise ValueError("Migration verification failed")
    return manifest


def durable_plan(runtime, records, source_type, source_identifier, source_sha256):
    try:
        planned = plan(records, runtime.cfg)
    except Exception as exc:
        runtime.ledger.save_manifest({"migration_id": str(uuid.uuid4()), "plugin_version": VERSION,
            "source_type": source_type, "source_path_or_collection": source_identifier,
            "source_sha256": source_sha256, "target_collection": runtime.store.collection,
            "embedding_fingerprint": runtime.store.embedder.fingerprint, "planning_error": safe_error(exc),
            "failed": len(records), "processed": 0, "started_at": now(), "completed_at": None, "records": {}})
        raise
    return planned


def resume_manifest(runtime, planned, source_type, source_identifier, source_sha256):
    manifests = runtime.ledger.manifests()
    matches = [m for m in manifests if "planning_error" not in m and m["source_type"] == source_type and
               m["source_path_or_collection"] == source_identifier and m["source_sha256"] == source_sha256 and
               m["target_collection"] == runtime.store.collection and
               m["embedding_fingerprint"] == runtime.store.embedder.fingerprint]
    # A source collection has no file checksum; changed snapshots need a fresh plan.
    if matches and matches[-1]["source_plan_hash"] == digest(planned):
        return matches[-1]
    return None


def create_manifest(runtime, planned, source_type, source_identifier, source_sha256):
    manifest = {"migration_id": str(uuid.uuid4()), "plugin_version": VERSION, "schema_version": 1,
                    "source_type": source_type, "source_path_or_collection": source_identifier,
                    "source_sha256": source_sha256, "source_plan_hash": digest(planned),
                    "source_unique_count": len(planned), "target_collection": runtime.store.collection,
                    "embedding_fingerprint": runtime.store.embedder.fingerprint,
                    "embedding_provider": runtime.cfg["embedding"]["provider"],
                    "embedding_model": runtime.cfg["embedding"]["model"],
                    "embedding_dimensions": runtime.store.embedder.dimensions,
                    "started_at": now(), "completed_at": None, "records": {}}
    with runtime.lock:
        for identifier, value in planned.items():
            scope = Scope(value["user_id"], value["agent_id"])
            old = runtime.store.get(identifier, scope)
            open_write = runtime.ledger.has_open_operations(identifier)
            if old and digest(old.payload) == digest(value) and not open_write:
                action, key = "SKIP", None
            else:
                action = "UPDATE" if old else "ADD"
                key = runtime.operation(identifier, "UPSERT", value,
                                        "mem0:" + value["cloud_origin"]["id"], value["updated_at"],
                                        generation=manifest["migration_id"] if open_write else "")
            manifest["records"][identifier] = {"action": action, "operation_key": key,
                                              "payload_hash": digest(value), "scope": scope.as_dict()}
    runtime.ledger.save_manifest(manifest)
    return manifest


def update_manifest_counts(runtime, manifest, expected):
    counts = {"processed": 0, "added": 0, "updated": 0, "skipped": 0, "failed": 0}
    for record in manifest["records"].values():
        key = record["operation_key"]
        row = runtime.ledger.row("operations", key) if key else None
        status = row["status"] if row else ("MISSING" if key else "COMMITTED")
        if status == "COMMITTED":
            counts["processed"] += 1
            counts[{"ADD": "added", "UPDATE": "updated", "SKIP": "skipped"}[record["action"]]] += 1
        elif status in {"FAILED", "MISSING"}:
            counts["failed"] += 1
    manifest.update(counts)
    manifest["completed_at"] = now() if counts["processed"] == expected else None
    runtime.ledger.save_manifest(manifest)


def verify_manifest(store, manifest):
    if "planning_error" in manifest:
        return {"ok": False, "planning_error": manifest["planning_error"]}
    missing, mismatched = [], []
    for identifier, record in manifest["records"].items():
        point = store.get(identifier, Scope(**record["scope"]))
        if point is None:
            missing.append(identifier)
        elif digest(point.payload) != record["payload_hash"]:
            mismatched.append(identifier)
    return {"ok": not missing and not mismatched, "expected": len(manifest["records"]),
            "target_exact_count": store.count(), "missing": missing, "mismatched": mismatched}


def verify_collection(store):
    failures = []
    count = 0
    for row in store.scroll(with_vectors=True):
        count += 1
        value = row.payload
        try:
            required = ("text", "user_id", "agent_id", "category", "source", "created_at", "updated_at",
                        "session_id", "importance", "content_hash", "schema_version", "cloud_origin")
            if not all(k in value for k in required) or value["schema_version"] != 1:
                raise ValueError("Payload contract mismatch")
            if value["content_hash"] != content_hash(value["text"]):
                raise ValueError("Hash mismatch")
            from .embedding import validate_vectors
            validate_vectors([row.vector["dense"]], 1, store.embedder.dimensions)
        except (ValueError, KeyError, TypeError):
            failures.append(str(row.id))
    return {"ok": not failures and count == store.count(), "checked": count,
            "target_exact_count": store.count(), "invalid_ids": failures}
