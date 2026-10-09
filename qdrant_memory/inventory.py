"""Read-only memory inventory and portable export documents."""

import contextlib
import json
import os
import shutil
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
    # Always export the scope's agent explicitly: an omitted field would let an
    # importer substitute its default agent (the profile scope) and silently
    # move no-agent memories into that agent's scope.
    record["agent_id"] = value.get("agent_id")
    metadata = value.get("metadata")
    if isinstance(metadata, dict) and origin.get("provider") == "mem0":
        # Round-trip fidelity: a Mem0-origin payload stores its source metadata
        # under "legacy_mem0"; export the original object so re-import does not
        # nest the wrapper inside another wrapper.
        metadata = metadata.get("legacy_mem0", metadata)
    if metadata:
        record["metadata"] = metadata
    return record


def write_export(path, collection, points, force=False):
    """Stream stored memories to a portable export atomically, owner-only.

    Records are written incrementally so large scopes never materialize a full
    document in memory. An existing target is refused unless overwrite is
    explicitly authorized; the refusal is atomic (hard-link publication, with
    an exclusive-create fallback where hard links are unavailable). Returns
    the number of exported records.
    """
    path = Path(path)
    if path.exists() and not force:
        raise ExportTargetExistsError("Export target already exists; pass --force to overwrite it")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    count = 0
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(
                "{\n"
                '  "provider": "qdrant-memory",\n'
                f'  "format": {json.dumps(EXPORT_FORMAT)},\n'
                f'  "version": {EXPORT_VERSION},\n'
                f'  "exported_at": {json.dumps(now())},\n'
                f'  "collection": {json.dumps(collection)},\n'
                '  "memories": ['
            )
            first = True
            for point in points:
                record = export_record(point)
                if not first:
                    handle.write(",")
                handle.write("\n    " + json.dumps(record, ensure_ascii=False))
                first = False
                count += 1
            handle.write('\n  ],\n  "count": ' + str(count) + "\n}\n")
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    if force:
        os.replace(temporary, path)
    else:
        try:
            _publish_no_clobber(temporary, path)
        finally:
            # Best-effort cleanup: the published target is already correct.
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
    path.chmod(0o600)
    return count


def _publish_no_clobber(temporary, path):
    """Publish a written temporary file without ever overwriting a target.

    Hard-link publication is atomic. Filesystems without hard links (FAT,
    exFAT, some network mounts) fall back to an exclusive create-and-copy,
    which still refuses to clobber an existing target.
    """
    try:
        os.link(temporary, path)
        return
    except FileExistsError:
        pass
    except OSError:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                with os.fdopen(descriptor, "wb") as target, open(temporary, "rb") as source:
                    shutil.copyfileobj(source, target)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            return
    raise ExportTargetExistsError("Export target already exists; pass --force to overwrite it")
