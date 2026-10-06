"""Stable payload, identity, size and source-version contracts."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import unicodedata
import uuid

NAMESPACE = uuid.UUID("0e039c91-9509-57b6-877a-e878e1d61d93")
UTC_OFFSET = "+00:00"


def now():
    return datetime.now(timezone.utc).isoformat().replace(UTC_OFFSET, "Z")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).split())


def content_hash(text):
    return "sha256:" + hashlib.sha256(normalize(text).encode()).hexdigest()


@dataclass(frozen=True)
class Scope:
    user_id: str
    agent_id: str | None

    def as_dict(self):
        return {"user_id": self.user_id, "agent_id": self.agent_id}


def point_id(scope, source, source_id):
    return str(uuid.uuid5(NAMESPACE, json.dumps([scope.user_id, scope.agent_id, source, source_id], ensure_ascii=False)))


def timestamp(value):
    if value is None:
        return now()
    parsed = datetime.fromisoformat(str(value).replace("Z", UTC_OFFSET))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace(UTC_OFFSET, "Z")


def payload(text, scope, source, session_id="", category=None, importance=0.5,
            metadata=None, cloud_origin=None, created_at=None, updated_at=None):
    if not isinstance(text, str) or not normalize(text):
        raise ValueError("Memory text must be nonempty")
    if not isinstance(importance, (float, int)) or not math.isfinite(importance) or not 0 <= importance <= 1:
        raise ValueError("Importance must be between zero and one")
    category = category or []
    if not isinstance(category, list) or not all(isinstance(x, str) for x in category):
        raise ValueError("category must be an array of strings")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    return {"text": text, **scope.as_dict(), "source": source, "session_id": session_id or None,
            "category": category, "importance": importance, "metadata": metadata or {},
            "cloud_origin": cloud_origin, "created_at": timestamp(created_at),
            "updated_at": timestamp(updated_at), "content_hash": content_hash(text), "schema_version": 1}


def enforce_limits(value, limits):
    value = dict(value)
    if len(value["text"].encode()) > limits["max_text_bytes"]:
        if limits["oversize_policy"] != "truncate":
            raise ValueError("Memory exceeds max_text_bytes")
        value["text"] = value["text"].encode()[:limits["max_text_bytes"]].decode("utf-8", errors="ignore")
        value["content_hash"] = content_hash(value["text"])
        value["metadata"] = {**value["metadata"], "truncated": True}
    for key, bound in (("metadata", "max_metadata_bytes"), (None, "max_payload_bytes")):
        if len(json.dumps(value[key] if key else value, ensure_ascii=False).encode()) > limits[bound]:
            raise ValueError(f"Memory exceeds {bound}")
    return value
