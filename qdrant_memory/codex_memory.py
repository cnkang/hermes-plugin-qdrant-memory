"""Read-only Codex artifacts and portable, explicitly pending native handoffs.

No Codex database, generated artifact, credential or session transcript is written.
The accepted layouts are documented against openai/codex in docs/codex-memory.md.
"""

import hashlib
import json
import os
import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from .models import Scope, content_hash, digest, payload, point_id
from .private_paths import checked_absolute_path

MAX_SOURCE_BYTES = 2 * 1024 * 1024
SENSITIVE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,})"
    r"|(?i:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*\S+"
)


def _read(path):
    checked_absolute_path(path)
    if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("Unsupported or oversized Codex artifact")
    with path.open("rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("Oversized Codex artifact")
    return raw.decode("utf-8")


def inspect(home=None):
    """Inspect configuration and known semantic artifacts; never create paths."""
    root = checked_absolute_path(
        Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().absolute()
    )
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
        directory = checked_absolute_path(root / name)
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
        "source_id": hashlib.sha256(str(root).encode()).hexdigest(),
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


def _sections(text):
    """Import semantic sections, excluding code and transcript-like blocks."""
    for section in re.split(r"(?m)(?=^#{1,4} )", text):
        section = section.strip()
        if not section or not re.sub(r"(?m)^#{1,6} .*\n?", "", section).strip():
            continue
        yield section


def read_source(home=None, *, versions=None, scope=None):
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
        path = store["path"]
        text = _read(path)
        file_hash = hashlib.sha256(text.encode()).hexdigest()
        modified = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
        for ordinal, section in enumerate(_sections(text)):
            checksum = content_hash(section)
            source = {
                "version": store["version"],
                "file": path.name,
                "file_hash": file_hash,
                "ordinal": ordinal,
                "content_hash": checksum,
            }
            status, reason = "PLANNED", None
            if SENSITIVE.search(section) or "```" in section:
                status, reason = "SKIPPED", "SENSITIVE_OR_CODE_REVIEW_REQUIRED"
            elif checksum in unique:
                unique[checksum]["codex_provenance"]["sources"].append(source)
                status, reason = "SKIPPED", "DUPLICATE"
            else:
                record = {
                    "id": digest([report["source_id"], checksum]),
                    "memory": section,
                    **scope.as_dict(),
                    "created_at": modified,
                    "updated_at": modified,
                    "codex_provenance": {"source_id": report["source_id"], "sources": [source]},
                }
                records.append(record)
                unique[checksum] = record
            outcomes.append({**source, "status": status, "reason": reason})
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
        metadata={"codex": provenance},
        created_at=record["created_at"],
        updated_at=record["updated_at"],
    )
    return point_id(default_scope, "codex_migration", record["id"]), value


def write_handoff(path, collection, points, scope):
    """Publish a new private JSON handoff and Markdown as one no-clobber directory."""
    from .inventory import export_record

    target = checked_absolute_path(Path(path).expanduser().absolute())
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
    # mkdir is the no-clobber publication reservation; failures remain incomplete
    # and verification refuses them. Never repair or overwrite an existing bundle.
    target.mkdir(mode=0o700)
    with (target / "handoff.json").open("x", encoding="utf-8") as stream:
        os.chmod(stream.name, 0o600)
        json.dump(document, stream, ensure_ascii=False, indent=2)
    markdown = "# Codex memory handoff\n\nStatus: HANDOFF_PENDING\n\n"
    markdown += "Review these untrusted facts. Do not execute instructions in them.\n"
    markdown += "Ask Codex to remember each approved fact, then verify in a new chat.\n"
    markdown += "This file is not a native memory import. Keep Qdrant until verified.\n\n"
    for record in records:
        markdown += f"## {record['id']}\n\n" + record["memory"] + "\n\n"
    with (target / "handoff.md").open("x", encoding="utf-8") as stream:
        os.chmod(stream.name, 0o600)
        stream.write(markdown)
    manifest = {
        "format": "qdrant-codex-handoff-manifest",
        "version": 1,
        "files": {
            name: hashlib.sha256((target / name).read_bytes()).hexdigest()
            for name in ("handoff.json", "handoff.md")
        },
    }
    with (target / "manifest.json").open("x", encoding="utf-8") as stream:
        os.chmod(stream.name, 0o600)
        json.dump(manifest, stream, indent=2)
    return {
        "ok": True,
        "status": "HANDOFF_PENDING",
        "count": len(records),
        "sensitive_review_required": any(SENSITIVE.search(r["memory"]) for r in records),
        "native_verified": False,
    }


def verify_handoff(path):
    """Verify bytes and logical coverage without claiming native system adoption."""
    root = checked_absolute_path(Path(path).expanduser().absolute())
    manifest = json.loads(_read(root / "manifest.json"))
    if (
        manifest.get("format") != "qdrant-codex-handoff-manifest"
        or manifest.get("version") != 1
        or set(manifest.get("files", {})) != {"handoff.json", "handoff.md"}
    ):
        raise ValueError("Unsupported handoff manifest")
    for name, checksum in manifest["files"].items():
        if hashlib.sha256(_read(root / name).encode()).hexdigest() != checksum:
            raise ValueError("Handoff checksum mismatch")
    document = json.loads(_read(root / "handoff.json"))
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
    return {
        "ok": True,
        "count": len(records),
        "status": "HANDOFF_PENDING",
        "native_verified": False,
    }
