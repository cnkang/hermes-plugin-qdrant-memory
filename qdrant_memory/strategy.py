"""Desired, effective and observed Qdrant policies for one profile destination.

The policy record is the Runtime control plane for long-lived Qdrant state that
is not part of the point payload: the managed payload-index set, the recall
policy, the data schema version and the embedding fingerprint. It is bound to
the same profile/destination identity as the Ledger and written with the same
private, atomic publication rules.

A record never carries memory text, credentials or endpoint URLs. Inspecting it
must not create a profile, initialize a collection, write the Ledger, run
recovery or download a model: :func:`read` and :func:`explain_config` are pure
filesystem reads.
"""

import json
import os
import tempfile
from pathlib import Path

from .config import DEFAULTS, backend_destination, embedding_config, fingerprint, merge
from .models import digest
from .private_paths import checked_absolute_path

POLICY_SCHEMA_VERSION = 1
DATA_SCHEMA_VERSION = 1
INDEX_POLICY_VERSION = 1
RECALL_POLICY_VERSION = 1
RECALL_POLICY_NAME = "dense"

# Frozen 0.6.x defaults. A legacy profile that predates the policy record is
# interpreted with these values, so a later default change cannot silently
# re-interpret an existing destination.
LEGACY_DEFAULTS = json.loads(json.dumps(DEFAULTS))

MISSING = "missing"
EQUIVALENT = "equivalent"
CONFLICT = "conflict"
UNMANAGED = "unmanaged"
UNSUPPORTED = "unsupported"
NOT_APPLICABLE = "not_applicable"

REQUIRED_FIELDS = (
    "scope.user_id",
    "scope.agent_id",
    "qdrant.mode",
    "qdrant.collection",
    "embedding.provider",
    "embedding.model",
    "embedding.dimensions",
    "embedding.distance",
    "search.mode",
    "search.top_k",
    "search.candidate_k",
    "search.min_score",
    "search.hnsw_ef",
    "search.exact",
    "dedupe.similarity_review_threshold",
    "dedupe.high_similarity_threshold",
    "limits.oversize_policy",
)


def index_policy():
    """Return the managed payload-index policy shared by every maintenance path."""
    from .qdrant_store import INDEXES

    return {
        "version": INDEX_POLICY_VERSION,
        "indexes": {key: {"type": kind} for key, kind in sorted(INDEXES.items())},
    }


def desired_policy(cfg):
    """Return the target definition an upgraded destination must be checked against."""
    emb = embedding_config(cfg)
    return {
        "policy_schema_version": POLICY_SCHEMA_VERSION,
        "config_schema_version": cfg["schema_version"],
        "data_schema_version": DATA_SCHEMA_VERSION,
        "index_policy_version": INDEX_POLICY_VERSION,
        "recall_policy_version": RECALL_POLICY_VERSION,
        "recall_policy": RECALL_POLICY_NAME,
        "embedding_fingerprint": fingerprint(emb),
        "dimensions": emb["dimensions"],
        "distance": emb["distance"],
        "backend": cfg["qdrant"]["mode"],
        "collection": cfg["qdrant"]["collection"],
    }


def config_digest(cfg):
    """Hash non-secret effective settings so an upgrade plan cannot outlive them."""
    emb = embedding_config(cfg)
    value = {
        "scope": cfg["scope"],
        "qdrant": {
            "mode": cfg["qdrant"]["mode"],
            "collection": cfg["qdrant"]["collection"],
            "destination": list(backend_destination(cfg["qdrant"])),
        },
        "embedding": {
            key: emb.get(key)
            for key in ("mode", "provider", "model", "dimensions", "distance", "base_url")
        },
        "search": cfg["search"],
        "dedupe": cfg["dedupe"],
        "limits": cfg["limits"],
    }
    return digest(value)


def directory(home):
    """Return the private policy directory without creating it."""
    return Path(home).expanduser().resolve() / "qdrant-memory" / "strategy"


def record_path(home, namespace):
    """Return the destination-bound policy record path."""
    return directory(home) / (digest(namespace) + ".json")


def state_path(home, namespace, suffix):
    """Return a destination-bound control-plane file path for one record kind."""
    return directory(home) / f"{digest(namespace)}.{suffix}.json"


def read(home, namespace):
    """Read a policy record without creating a directory or a file.

    Returns ``None`` when the destination has no record. Unreadable records are
    returned as ``{"corrupt": True, ...}`` so callers report a blocker instead
    of treating the destination as freshly installed.
    """
    path = record_path(home, namespace)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return {"corrupt": True}
    if not isinstance(value, dict):
        return {"corrupt": True}
    return value


def _publish(path, value):
    """Atomically publish one private JSON record under the profile directory."""
    directory_path = path.parent
    checked_absolute_path(directory_path.parent)
    directory_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".policy-", dir=directory_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def write(home, namespace, record):
    """Atomically publish a policy record under the private profile directory."""
    return _publish(record_path(home, namespace), record)


def write_state(home, namespace, suffix, record):
    """Atomically publish one control-plane record such as an upgrade plan."""
    return _publish(state_path(home, namespace, suffix), record)


def read_state(home, namespace, suffix):
    """Read one control-plane record without creating a directory or a file."""
    path = state_path(home, namespace, suffix)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _observed_type(value):
    """Normalize a REST/gRPC payload-schema type into the policy vocabulary."""
    raw = getattr(value, "value", value)
    text = str(raw).lower()
    if text in {"datetime", "date"}:
        return "datetime"
    if text in {"integer", "int"}:
        return "integer"
    return text


def observe(client, collection, mode):
    """Read the collection's actual payload indexes and compare them by field.

    Embedded storage has no server-side payload indexes, so the result is
    ``not_applicable`` rather than a simulated success. Additional indexes that
    the policy does not manage are reported as ``unmanaged`` and never removed.
    """
    from .qdrant_store import is_local_client

    if mode == "embedded" or is_local_client(client):
        return {"backend": "embedded", "status": NOT_APPLICABLE, "entries": [], "extra": []}
    info = client.get_collection(collection)
    observed = getattr(info, "payload_schema", None) or {}
    policy = index_policy()["indexes"]
    entries = []
    for field, definition in sorted(policy.items()):
        expected = definition["type"]
        found = observed.get(field)
        if found is None:
            entries.append(
                {"field": field, "expected": expected, "observed": None, "status": MISSING}
            )
            continue
        actual = _observed_type(getattr(found, "data_type", None))
        status = EQUIVALENT if actual == expected else CONFLICT
        entries.append({"field": field, "expected": expected, "observed": actual, "status": status})
    extra = [
        {"field": field, "observed": _observed_type(getattr(value, "data_type", None))}
        for field, value in sorted(observed.items())
        if field not in policy
    ]
    statuses = {entry["status"] for entry in entries}
    if CONFLICT in statuses:
        status = CONFLICT
    elif MISSING in statuses:
        status = MISSING
    else:
        status = EQUIVALENT
    return {"backend": "remote", "status": status, "entries": entries, "extra": extra}


def managed_drift(observation):
    """Return the managed fields that are missing or conflicting."""
    return [
        entry for entry in observation.get("entries", []) if entry["status"] in {MISSING, CONFLICT}
    ]


def explain_config(home, cfg, *, defaults=None):
    """Report where each effective dense-policy value came from.

    The profile file, the frozen 0.6.x defaults, an inherited embedding fallback
    and a one-off override are distinguished so a value that merely equals the
    old default is never treated as an explicit user choice.
    """
    base = LEGACY_DEFAULTS if defaults is None else defaults
    path = Path(home).expanduser().resolve() / "qdrant-memory.json"
    values = {}
    if path.is_file() and not path.is_symlink():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            values = loaded if isinstance(loaded, dict) else {}
        except (ValueError, OSError):
            values = {}
    sources = {}
    for dotted in REQUIRED_FIELDS:
        group, key = dotted.split(".", 1)
        if group == "embedding" and cfg["embedding"]["mode"] == "inherit":
            origin = "inherit_fallback"
        elif isinstance(values.get(group), dict) and key in values[group]:
            origin = "profile"
        else:
            origin = "default"
        sources[dotted] = {
            "value": _lookup(cfg, dotted),
            "source": origin,
            "legacy_default": _lookup(base, dotted),
        }
    return {
        "profile_file": str(path),
        "profile_present": bool(values),
        "sources": sources,
    }


def _lookup(cfg, dotted):
    """Read a dotted setting from a merged configuration."""
    value = cfg
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def legacy_effective(cfg):
    """Describe what a pre-policy destination keeps doing after the upgrade.

    A destination with no policy record continues to serve its existing dense
    path. The record only marks it as legacy until an upgrade plan is applied;
    no collection, payload or fingerprint is rewritten.
    """
    return {
        "status": "legacy",
        "recall_policy": RECALL_POLICY_NAME,
        "data_schema_version": DATA_SCHEMA_VERSION,
        "index_policy_version": None,
        "accepted": False,
    }


def matches_target(record, target):
    """Return whether an existing record already encodes the desired policy."""
    if not isinstance(record, dict) or record.get("corrupt"):
        return False
    policy = record.get("policy")
    if not isinstance(policy, dict):
        return False
    keys = (
        "policy_schema_version",
        "data_schema_version",
        "index_policy_version",
        "recall_policy_version",
        "recall_policy",
        "embedding_fingerprint",
    )
    return all(policy.get(key) == target.get(key) for key in keys)


def effective_summary(home, namespace, cfg):
    """Return the current effective policy and how it compares with the target."""
    record = read(home, namespace)
    target = desired_policy(cfg)
    if record is None:
        return {
            "status": "missing",
            "effective": legacy_effective(cfg),
            "target": target,
            "record": None,
        }
    if record.get("corrupt"):
        return {
            "status": "corrupt",
            "effective": None,
            "target": target,
            "record": None,
        }
    policy = record.get("policy") if isinstance(record.get("policy"), dict) else {}
    status = "accepted" if matches_target(record, target) else "pending"
    return {
        "status": status,
        "effective": policy or legacy_effective(cfg),
        "target": target,
        "record": {
            key: record.get(key)
            for key in ("generation", "activated_at", "updated_at")
            if key in record
        },
    }


def frozen_defaults_match(defaults=None):
    """Return whether the live defaults still equal the frozen 0.6.x dense policy."""
    current = DEFAULTS if defaults is None else defaults
    keys = ("schema_version", "scope", "qdrant", "embedding", "search", "dedupe", "limits")
    return all(current.get(key) == LEGACY_DEFAULTS.get(key) for key in keys)


def merged_legacy(values, overrides=None):
    """Reconstruct a legacy destination's effective settings from frozen defaults."""
    return merge(merge(LEGACY_DEFAULTS, values or {}), overrides or {})
