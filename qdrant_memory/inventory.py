"""Read-only memory inventory and portable export documents."""

import json
import os
import tempfile
from pathlib import Path

from .models import now

EXPORT_FORMAT = "qdrant-memory-export"
EXPORT_VERSION = 1


class ExportTargetExistsError(ValueError):
    """Refuse to overwrite an existing export without explicit authorization."""


def memory_view(point):
    """Return one stored memory as a scope-annotated inventory record.

    The reserved schema identity point never reaches this view because the
    store's scroll excludes it from memory records.
    """
    value = point.payload or {}
    return {
        "point_id": str(point.id),
        "user_id": value.get("user_id"),
        "agent_id": value.get("agent_id"),
        "text": value.get("text"),
        "source": value.get("source"),
        "session_id": value.get("session_id"),
        "category": value.get("category") or [],
        "importance": value.get("importance"),
        "created_at": value.get("created_at"),
        "updated_at": value.get("updated_at"),
        "content_hash": value.get("content_hash"),
        "cloud_origin": value.get("cloud_origin"),
    }


def export_record(point):
    """Return one stored memory in the shape the Mem0 JSON importer accepts.

    Round trips preserve text, scope, categories and timestamps. Memories
    migrated from Mem0 export their original Mem0 ID and re-import to the same
    point; native memories export their point UUID for provenance, and
    re-imports re-embed and derive fresh point IDs. Vectors are never exported.
    """
    value = point.payload or {}
    origin = value.get("cloud_origin")
    origin = origin if isinstance(origin, dict) else {}
    record = {
        "id": origin.get("id") or str(point.id),
        "memory": value.get("text", ""),
        "user_id": value.get("user_id"),
        "created_at": value.get("created_at"),
        "updated_at": value.get("updated_at"),
        "categories": value.get("category") or [],
        "hash": value.get("content_hash"),
    }
    if value.get("agent_id") is not None:
        record["agent_id"] = value["agent_id"]
    metadata = value.get("metadata")
    if isinstance(metadata, dict) and origin.get("provider") == "mem0":
        # Round-trip fidelity: a Mem0-origin payload stores its source metadata
        # under "legacy_mem0"; export the original object so re-import does not
        # nest the wrapper inside another wrapper.
        metadata = metadata.get("legacy_mem0", metadata)
    if metadata:
        record["metadata"] = metadata
    return record


def export_document(points, collection):
    """Build the portable export envelope accepted by ``migrate mem0``."""
    records = [export_record(point) for point in points]
    return {
        "provider": "qdrant-memory",
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exported_at": now(),
        "collection": collection,
        "count": len(records),
        "memories": records,
    }


def write_export(path, document, force=False):
    """Write an export atomically with owner-only permissions.

    An existing target is refused unless overwrite is explicitly authorized;
    partial writes never remain visible under the final path.
    """
    path = Path(path)
    if path.exists() and not force:
        raise ExportTargetExistsError("Export target already exists; pass --force to overwrite it")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, path)
    path.chmod(0o600)
    return path
