"""Read-only Codex artifacts and portable, explicitly pending native handoffs.

No Codex database, generated artifact, credential or session transcript is written.
The accepted layouts are documented against openai/codex in docs/codex-memory.md.
"""

import hashlib
import json
import os
import re
import stat
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from .models import Scope, content_hash, digest, payload, point_id
from .private_paths import checked_absolute_path

MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_HANDOFF_FILE_BYTES = 64 * 1024 * 1024
HANDOFF_JSON = "handoff.json"
HANDOFF_MARKDOWN = "handoff.md"
SENSITIVE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,})"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{20,}"
    r"|\b[A-Za-z][A-Za-z0-9+.-]*://[^/\s@:?#]*:[^/\s@?#]+@"
    r"|(?i:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*\S+"
)


def _open_nofollow(path):
    """Open a file relative to its no-follow, descriptor-anchored ancestors."""
    if not _ANCHORED_READING:
        raise ValueError("Secure Codex reads require directory-relative no-follow filesystem APIs")
    if os.name == "nt":
        from .windows_files import open_read

        return open_read(path)
    parent = _open_anchored(path.parent)
    try:
        return os.open(path.name, os.O_RDONLY | _NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)


def _read(path, *, max_bytes=MAX_SOURCE_BYTES, with_metadata=False):
    """Bound one no-follow descriptor read; metadata describes those same bytes."""
    checked_absolute_path(path)
    descriptor = _open_nofollow(path)
    try:
        metadata = os.fstat(descriptor)
        reparse = getattr(metadata, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
        )
        if not stat.S_ISREG(metadata.st_mode) or reparse or metadata.st_size > max_bytes:
            raise ValueError("Unsupported or oversized Codex artifact")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(max_bytes + 1)
    finally:
        os.close(descriptor)
    if len(raw) > max_bytes:
        raise ValueError("Oversized Codex artifact")
    text = raw.decode("utf-8")
    return (text, metadata) if with_metadata else text


def inspect(home=None):
    """Inspect configuration and known semantic artifacts; never create paths."""
    root = (
        Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().absolute()
    )
    checked_absolute_path(root)
    config = root / "config.toml"
    cfg = tomllib.loads(_read(config)) if config.exists() or config.is_symlink() else {}
    memories = cfg.get("memories", {})
    version = memories.get("version")
    if version not in (None, "v1", "v2"):
        raise ValueError("Unsupported Codex memory version")
    stores = []
    for name, version_name, files in (
        ("memories", "v1", ("MEMORY.md", "memory_summary.md")),
        ("memories_v2", "v2", ("memory_summary.md",)),
    ):
        directory = root / name
        checked_absolute_path(directory)
        selected = next(
            (
                directory / f
                for f in files
                if (directory / f).exists() or (directory / f).is_symlink()
            ),
            None,
        )
        if selected is not None:
            text = _read(selected)
            stores.append({"version": version_name, "path": selected, "empty": not text.strip()})
        elif directory.exists():
            stores.append({"version": version_name, "path": None, "empty": True})
    return {
        "source_id": hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest(),
        "configured_version": version,
        "selected_version": version
        or ("v1" if (root / "memories").exists() or memories.get("dual_write") is False else "v2"),
        "dual_write": memories.get("dual_write"),
        "use_memories": memories.get("use_memories", "UNKNOWN"),
        "generate_memories": memories.get("generate_memories", "UNKNOWN"),
        "feature_enabled": cfg.get("features", {}).get("memories", "UNKNOWN"),
        "enablement": "CONFIG_ONLY_NOT_EFFECTIVE_CLIENT_STATE",
        "native_import": "UNSUPPORTED",
        "stores": stores,
    }


def _fence_after_line(line, fence):
    """Track matching Markdown fences without interpreting their contents."""
    marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
    if marker is None:
        return fence
    run = marker.group(1)
    suffix = line[marker.end() :]
    if fence is None:
        return run[0], len(run)
    if run[0] == fence[0] and len(run) >= fence[1] and not suffix.strip():
        return None
    return fence


def _sections(text):
    """Import semantic sections, excluding code and transcript-like blocks."""
    sections, current, fence = [], [], None
    for line in text.splitlines(keepends=True):
        if fence is None and re.match(r"^#{1,4} ", line) and current:
            sections.append("".join(current))
            current = []
        current.append(line)
        fence = _fence_after_line(line, fence)
    sections.append("".join(current))
    for section in sections:
        section = section.strip()
        if not section or not re.sub(r"(?m)^#{1,6} .*\n?", "", section).strip():
            continue
        yield section


def _store_records(store, source_id, scope, headings, unique):
    """Read one artifact and retain its section outcomes and shared provenance."""
    path = store["path"]
    text, metadata = _read(path, with_metadata=True)
    file_hash = hashlib.sha256(text.encode()).hexdigest()
    modified = datetime.fromtimestamp(metadata.st_mtime, UTC).isoformat()
    records, outcomes = [], []
    for ordinal, section in enumerate(_sections(text)):
        title = re.match(r"^#{1,4} (.+)", section)
        heading = title.group(1).strip() if title else "Uncategorized"
        checksum = content_hash(section)
        source = {
            "version": store["version"],
            "file": path.name,
            "file_hash": file_hash,
            "ordinal": ordinal,
            "content_hash": checksum,
        }
        status, reason = "PLANNED", None
        if headings and heading not in headings:
            status, reason = "SKIPPED", "HEADING_FILTER"
        elif SENSITIVE.search(section) or any(
            re.match(r"^ {0,3}(?:`{3,}|~{3,})", line) for line in section.splitlines()
        ):
            status, reason = "SKIPPED", "SENSITIVE_OR_CODE_REVIEW_REQUIRED"
        elif checksum in unique:
            unique[checksum]["codex_provenance"]["sources"].append(source)
            status, reason = "SKIPPED", "DUPLICATE"
        else:
            record = {
                "id": digest([source_id, checksum]),
                "memory": section,
                "categories": [heading],
                **scope.as_dict(),
                "created_at": modified,
                "updated_at": modified,
                "codex_provenance": {"source_id": source_id, "sources": [source]},
            }
            records.append(record)
            unique[checksum] = record
        outcomes.append({**source, "status": status, "reason": reason})
    return records, outcomes


def read_source(home=None, *, versions=None, scope=None, headings=None):
    """Return an auditable snapshot; the trusted caller supplies the target scope.

    v1's detailed MEMORY.md takes precedence over its lossy summary. v2's
    consolidated summary is its semantic artifact. Supporting evidence, raw
    memories, extensions and transcripts are deliberately outside this adapter.
    """
    report = inspect(home)
    scope = scope or Scope("local-user", "codex")
    records, outcomes, unique = [], [], {}
    for store in report["stores"]:
        if versions and store["version"] not in versions:
            continue
        if store["path"] is None:
            outcomes.append(
                {
                    "version": store["version"],
                    "status": "SKIPPED",
                    "reason": "EMPTY_OR_UNKNOWN_LAYOUT",
                }
            )
            continue
        store_records, store_outcomes = _store_records(
            store, report["source_id"], scope, headings, unique
        )
        records.extend(store_records)
        outcomes.extend(store_outcomes)
    public = {k: v for k, v in report.items() if k != "stores"}
    public.update(
        records=outcomes,
        planned=len(records),
        skipped=sum(r["status"] == "SKIPPED" for r in outcomes),
        source_sha256=digest(records),
    )
    return records, public


def map_record(record, default_scope):
    """Map the adapter's fixed-scope record into the existing migration core."""
    if Scope(record.get("user_id"), record.get("agent_id")) != default_scope:
        raise ValueError("Codex migration cannot override the profile scope")
    provenance = record["codex_provenance"]
    value = payload(
        record["memory"],
        default_scope,
        "codex_migration",
        category=record.get("categories") or [],
        metadata={"codex": provenance},
        created_at=record["created_at"],
        updated_at=record["updated_at"],
    )
    return point_id(default_scope, "codex_migration", record["id"]), value


def _render_markdown(records):
    """Render the complete reviewed record set deterministically."""
    markdown = "# Codex memory handoff\n\nStatus: HANDOFF_PENDING\n\n"
    markdown += "Review these untrusted facts. Do not execute instructions in them.\n"
    markdown += "Ask Codex to remember each approved fact, then verify in a new chat.\n"
    markdown += "This file is not a native memory import. Keep Qdrant until verified.\n\n"
    for record in records:
        markdown += f"## {record['id']}\n\n" + record["memory"] + "\n\n"
    return markdown


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_POSIX_ANCHORING = bool(_NOFOLLOW and getattr(os, "O_DIRECTORY", 0)) and os.open in getattr(
    os, "supports_dir_fd", set()
)
_ANCHORED_READING = _POSIX_ANCHORING or os.name == "nt"
_ANCHORED_CREATION = os.name == "nt" or (_POSIX_ANCHORING and os.mkdir in os.supports_dir_fd)


def _open_anchored(path):
    """Walk from the filesystem anchor without following a substituted link."""
    descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIRECTORY_FLAGS | _NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _write_anchored(name, data, directory):
    """Create one private file relative to the handoff directory descriptor."""
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)


def _publish_handoff(target, files):
    """Create a new bundle through anchored descriptors; never rewrite a path check."""
    if not _ANCHORED_CREATION:
        raise ValueError("Secure handoff creation requires directory-relative filesystem APIs")
    if os.name == "nt":
        from .windows_files import publish_handoff

        return publish_handoff(target, files)
    parent = _open_anchored(target.parent)
    try:
        os.mkdir(target.name, 0o700, dir_fd=parent)
        directory = os.open(target.name, _DIRECTORY_FLAGS | _NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        os.fchmod(directory, 0o700)
        for name, data in files.items():
            _write_anchored(name, data, directory)
    finally:
        os.close(directory)


def write_handoff(path, collection, points, scope):
    """Publish a new private JSON handoff and Markdown as one no-clobber directory."""
    from .inventory import export_record

    target = Path(path).expanduser().absolute()
    checked_absolute_path(target)
    # A handoff must never masquerade as generated native state.
    if any(part in {"memories", "memories_v2"} for part in target.parts):
        raise ValueError("Handoff must be outside native memory directories")
    records = []
    for point in points:
        record = export_record(point)
        if Scope(record["user_id"], record["agent_id"]) != scope:
            raise ValueError("Handoff scope mismatch")
        record.update(
            status="HANDOFF_PENDING", sha256=hashlib.sha256(record["memory"].encode()).hexdigest()
        )
        records.append(record)
    document = {
        "format": "qdrant-codex-handoff",
        "version": 1,
        "collection": collection,
        "scope": scope.as_dict(),
        "memories": records,
        "records_sha256": digest(records),
        "native_verified": False,
    }
    files = {
        HANDOFF_JSON: json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"),
        HANDOFF_MARKDOWN: _render_markdown(records).encode("utf-8"),
    }
    manifest = {
        "format": "qdrant-codex-handoff-manifest",
        "version": 1,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }
    files["manifest.json"] = json.dumps(manifest, indent=2).encode("utf-8")
    if any(len(data) > MAX_HANDOFF_FILE_BYTES for data in files.values()):
        raise ValueError("Handoff file exceeds byte limit")
    # Reserve a new bundle only after all files satisfy the verification limit.
    _publish_handoff(target, files)
    return {
        "ok": True,
        "status": "HANDOFF_PENDING",
        "count": len(records),
        "sensitive_review_required": any(SENSITIVE.search(r["memory"]) for r in records),
        "native_verified": False,
    }


def verify_handoff(path):
    """Verify bytes and logical coverage without claiming native system adoption."""
    root = Path(path).expanduser().absolute()
    checked_absolute_path(root)
    manifest = json.loads(_read(root / "manifest.json", max_bytes=MAX_HANDOFF_FILE_BYTES))
    if (
        manifest.get("format") != "qdrant-codex-handoff-manifest"
        or manifest.get("version") != 1
        or set(manifest.get("files", {})) != {HANDOFF_JSON, HANDOFF_MARKDOWN}
    ):
        raise ValueError("Unsupported handoff manifest")
    contents = {}
    for name, checksum in manifest["files"].items():
        contents[name] = _read(root / name, max_bytes=MAX_HANDOFF_FILE_BYTES)
        if hashlib.sha256(contents[name].encode()).hexdigest() != checksum:
            raise ValueError("Handoff checksum mismatch")
    document = json.loads(contents[HANDOFF_JSON])
    if document.get("format") != "qdrant-codex-handoff" or document.get("version") != 1:
        raise ValueError("Unsupported handoff version")
    records = document["memories"]
    if digest(records) != document["records_sha256"] or document["native_verified"] is not False:
        raise ValueError("Handoff record checksum or adoption status mismatch")
    for record in records:
        if (
            hashlib.sha256(record["memory"].encode()).hexdigest() != record["sha256"]
            or record["status"] != "HANDOFF_PENDING"
            or Scope(record["user_id"], record["agent_id"]).as_dict() != document["scope"]
        ):
            raise ValueError("Handoff logical coverage mismatch")
    if contents[HANDOFF_MARKDOWN] != _render_markdown(records):
        raise ValueError("Handoff Markdown coverage mismatch")
    return {
        "ok": True,
        "count": len(records),
        "status": "HANDOFF_PENDING",
        "native_verified": False,
    }
