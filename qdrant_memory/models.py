"""Stable payload, identity, size and source-version contracts."""

import hashlib
import json
import math
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

NAMESPACE = uuid.UUID("0e039c91-9509-57b6-877a-e878e1d61d93")
UTC_OFFSET = "+00:00"


def now():
    """Return the current UTC instant as an RFC3339 string."""
    return datetime.now(UTC).isoformat().replace(UTC_OFFSET, "Z")


def digest(value):
    """Hash canonical JSON for durable identity and verification."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def normalize(text):
    """Normalize Unicode and whitespace without changing stored display text."""
    return " ".join(unicodedata.normalize("NFKC", text).split())


def content_hash(text):
    """Hash normalized text for exact scoped duplicate detection."""
    return "sha256:" + hashlib.sha256(normalize(text).encode()).hexdigest()


@dataclass(frozen=True)
class Scope:
    """Identify a memory partition by user and optional agent identity."""

    user_id: str
    agent_id: str | None

    def as_dict(self):
        """Return the payload fields used by every scope check."""
        return {"user_id": self.user_id, "agent_id": self.agent_id}


def point_id(scope, source, source_id):
    """Derive a deterministic UUID from scope, source and source identity."""
    return str(
        uuid.uuid5(
            NAMESPACE,
            json.dumps([scope.user_id, scope.agent_id, source, source_id], ensure_ascii=False),
        )
    )


def timestamp(value):
    """Normalize a timestamp to UTC, treating naive values as UTC."""
    if value is None:
        return now()
    parsed = datetime.fromisoformat(str(value).replace("Z", UTC_OFFSET))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace(UTC_OFFSET, "Z")


def payload(
    text,
    scope,
    source,
    session_id="",
    category=None,
    importance=0.5,
    metadata=None,
    cloud_origin=None,
    created_at=None,
    updated_at=None,
):
    """Build a validated schema-v1 payload with provenance and stable text hashing."""
    if not isinstance(text, str) or not normalize(text):
        raise ValueError("Memory text must be nonempty")
    if (
        not isinstance(importance, (float, int))
        or not math.isfinite(importance)
        or not 0 <= importance <= 1
    ):
        raise ValueError("Importance must be between zero and one")
    category = category or []
    if not isinstance(category, list) or not all(isinstance(x, str) for x in category):
        raise ValueError("category must be an array of strings")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    return {
        "text": text,
        **scope.as_dict(),
        "source": source,
        "session_id": session_id or None,
        "category": category,
        "importance": importance,
        "metadata": metadata or {},
        "cloud_origin": cloud_origin,
        "created_at": timestamp(created_at),
        "updated_at": timestamp(updated_at),
        "content_hash": content_hash(text),
        "schema_version": 1,
    }


def enforce_limits(value, limits):
    """Enforce UTF-8 byte limits, rejecting oversize data unless truncation is explicit."""
    value = dict(value)
    if len(value["text"].encode()) > limits["max_text_bytes"]:
        if limits["oversize_policy"] != "truncate":
            raise ValueError("Memory exceeds max_text_bytes")
        value["text"] = (
            value["text"].encode()[: limits["max_text_bytes"]].decode("utf-8", errors="ignore")
        )
        if not normalize(value["text"]):
            raise ValueError("Memory text must be nonempty after truncation")
        value["content_hash"] = content_hash(value["text"])
        value["metadata"] = {**value["metadata"], "truncated": True}
    for key, bound in (("metadata", "max_metadata_bytes"), (None, "max_payload_bytes")):
        if (
            len(json.dumps(value[key] if key else value, ensure_ascii=False).encode())
            > limits[bound]
        ):
            raise ValueError(f"Memory exceeds {bound}")
    return value
