"""Profile-scoped configuration. Environment reads use Hermes's secret scope."""

import hashlib
import ipaddress
import json
import math
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

DEFAULTS = {
    "schema_version": 1,
    "scope": {"user_id": "hermes-user", "agent_id": "hermes"},
    "qdrant": {
        "mode": "embedded",
        "collection": "hermes_qdrant_memory",
        "url": None,
        "api_key": None,
        "url_env": "QDRANT_URL",
        "api_key_env": "QDRANT_API_KEY",
        "timeout_seconds": 5,
        "prefer_grpc": False,
    },
    "llm": {"mode": "inherit", "task": "qdrant_memory_extraction"},
    "embedding": {
        "mode": "plugin",
        "provider": "ollama",
        "model": "qwen3-embedding:4b",
        "base_url": "http://127.0.0.1:11434",
        "dimensions": 2560,
        "distance": "Cosine",
        "batch_size": 32,
        "send_dimensions": False,
        "api_key_env": "EMBEDDING_API_KEY",
        "timeout_seconds": 30,
    },
    "search": {
        "mode": "dense",
        "top_k": 8,
        "candidate_k": 24,
        "min_score": None,
        "hnsw_ef": 128,
        "exact": False,
        "rerank": False,
    },
    "dedupe": {
        "similarity_review_threshold": 0.92,
        "high_similarity_threshold": 0.97,
        "time_decay_half_life_days": 365,
    },
    "write": {
        "batch_size": 64,
        "max_attempts": 5,
        "backoff_base_seconds": 0.5,
        "backoff_max_seconds": 30,
        "shutdown_timeout_seconds": 5,
    },
    "limits": {
        "max_text_bytes": 65536,
        "max_metadata_bytes": 32768,
        "max_payload_bytes": 131072,
        "oversize_policy": "reject",
    },
}


def merge(base, values):
    """Deep-merge settings without modifying either input."""
    result = deepcopy(base)
    for key, value in values.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def secret(name):
    """Read a secret from the active Hermes profile's secret scope."""
    from agent.secret_scope import get_secret

    return get_secret(name)


def active_home():
    """Resolve the current profile's home at call time."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def validate_url(value, cloud=False, api_key=None):
    """Reject invalid endpoints, unsafe API-key transport and non-HTTPS Cloud URLs."""
    parsed = urlsplit(value or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Endpoint must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Credentials and query parameters are forbidden in endpoint URLs")
    if cloud and parsed.scheme != "https":
        raise ValueError("Cloud requires HTTPS")
    if api_key and parsed.scheme == "http":
        hostname = parsed.hostname.rstrip(".").lower()
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = False
        if hostname != "localhost" and not loopback:
            raise ValueError("API-key endpoints require HTTPS")


def validate(cfg):
    """Validate supported modes, numeric bounds and caller scope."""
    if cfg["schema_version"] != 1:
        raise ValueError("Unsupported configuration schema")
    if cfg["qdrant"]["mode"] not in {"embedded", "server", "cloud"}:
        raise ValueError("Invalid Qdrant mode")
    if not isinstance(cfg["qdrant"]["collection"], str) or not cfg["qdrant"]["collection"]:
        raise ValueError("Collection is required")
    validate_llm(cfg["llm"])
    emb = validate_embedding(cfg)
    validate_numbers(cfg, emb)
    validate_search_scope(cfg)


def validate_llm(llm):
    """Validate inherited, auxiliary-task and explicitly overridden LLM routing."""
    if llm["mode"] not in {"inherit", "task", "override"}:
        raise ValueError("Invalid LLM mode")
    if llm["mode"] == "task" and llm["task"] != "qdrant_memory_extraction":
        raise ValueError("Use the plugin's registered auxiliary task")
    if llm["mode"] == "override" and not all(llm.get(k) for k in ("provider", "model")):
        raise ValueError("LLM override requires provider and model")


def validate_embedding(cfg):
    """Validate embedding selection and return the effective pipeline settings."""
    if cfg["embedding"]["mode"] not in {"plugin", "inherit"}:
        raise ValueError("Invalid embedding mode")
    if cfg["embedding"]["mode"] == "inherit" and not cfg["embedding"].get("inherit_fallback"):
        raise ValueError("Embedding inheritance requires an explicit fallback")
    emb = embedding_config(cfg)
    if emb["provider"] not in {"ollama", "openai-compatible"}:
        raise ValueError("Unsupported embedding provider")
    validate_url(emb["base_url"], api_key=emb.get("api_key"))
    if emb["distance"] not in {"Cosine", "Dot", "Euclid"}:
        raise ValueError("Unsupported distance metric")
    return emb


def positive_number(group, key):
    """Require finite positive numbers and integer types for count fields."""
    value = group[key]
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{key} must be positive")
    if key != "timeout_seconds" and not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")


def validate_numbers(cfg, emb):
    """Validate batching, timeouts, retry delays, payload limits and thresholds."""
    positive = [
        (emb, "dimensions"),
        (emb, "batch_size"),
        (emb, "timeout_seconds"),
        (cfg["search"], "top_k"),
        (cfg["search"], "candidate_k"),
        (cfg["search"], "hnsw_ef"),
        (cfg["write"], "batch_size"),
        (cfg["write"], "max_attempts"),
        (cfg["qdrant"], "timeout_seconds"),
    ]
    positive += [
        (cfg["limits"], k) for k in ("max_text_bytes", "max_metadata_bytes", "max_payload_bytes")
    ]
    for group, key in positive:
        positive_number(group, key)
    for key in ("backoff_base_seconds", "backoff_max_seconds", "shutdown_timeout_seconds"):
        value = cfg["write"][key]
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{key} must be nonnegative and finite")
    for key in ("similarity_review_threshold", "high_similarity_threshold"):
        if not 0 <= cfg["dedupe"][key] <= 1:
            raise ValueError("Similarity threshold must be between zero and one")


def validate_search_scope(cfg):
    """Validate dense retrieval options, size policy and identity fields."""
    if cfg["search"]["mode"] != "dense" or cfg["search"]["rerank"]:
        raise ValueError("Hybrid retrieval and reranking are future capabilities")
    if cfg["search"]["candidate_k"] < cfg["search"]["top_k"]:
        raise ValueError("candidate_k must be at least top_k")
    if cfg["limits"]["oversize_policy"] not in {"reject", "truncate"}:
        raise ValueError("Invalid oversize policy")
    if not isinstance(cfg["scope"]["user_id"], str) or not cfg["scope"]["user_id"]:
        raise ValueError("A nonempty user_id is required")
    if cfg["scope"]["agent_id"] is not None and not isinstance(cfg["scope"]["agent_id"], str):
        raise ValueError("agent_id must be a string or null")


def load_config(home, overrides=None, resolve_secrets=True):
    """Load profile settings, merge defaults/overrides and resolve scoped secrets.

    Behavior comes from qdrant-memory.json. Secret resolution can be disabled for
    local inspection; remote endpoint validation runs when resolution is enabled.
    Invalid supported settings raise ValueError before service initialization.
    """
    home = Path(home).expanduser().resolve()
    path = home / "qdrant-memory.json"
    from utils import read_json_or_empty

    values = read_json_or_empty(path) if path.exists() else {}
    if not isinstance(values, dict):
        raise ValueError("Plugin configuration must be an object")
    cfg = merge(merge(DEFAULTS, values), overrides or {})
    validate(cfg)
    q = cfg["qdrant"]
    storage = Path(q.get("path") or "qdrant-memory/qdrant").expanduser()
    q["path"] = str((storage if storage.is_absolute() else home / storage).resolve())
    if resolve_secrets:
        resolve_qdrant_secrets(q)
    return cfg


def resolve_qdrant_secrets(q):
    """Resolve Qdrant endpoint and API key precedence in the active profile scope."""
    q["url"] = q.get("url") or secret(q["url_env"])
    q["api_key"] = q.get("api_key") or secret(q["api_key_env"])
    if q["mode"] != "embedded":
        q["url"] = q["url"] or ("http://127.0.0.1:6333" if q["mode"] == "server" else None)
        validate_url(q["url"], cloud=q["mode"] == "cloud", api_key=q["api_key"])
        if q["mode"] == "cloud" and not q["api_key"]:
            raise ValueError("Cloud requires a scoped QDRANT_API_KEY")


def embedding_config(cfg):
    """Return the explicit plugin pipeline or configured inheritance fallback."""
    emb = cfg["embedding"]
    return (
        merge(DEFAULTS["embedding"], emb.get("inherit_fallback", {}))
        if emb["mode"] == "inherit"
        else emb
    )


def fingerprint(emb):
    """Hash pipeline settings so incompatible embeddings cannot share a collection."""
    keys = (
        "provider",
        "model",
        "dimensions",
        "distance",
        "document_instruction",
        "query_instruction",
        "normalization_version",
        "base_url",
        "send_dimensions",
    )
    values = {k: emb.get(k) for k in keys}
    if isinstance(values["base_url"], str):
        values["base_url"] = values["base_url"].rstrip("/")
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def ledger_namespace(cfg):
    """Bind ledger work to a collection and canonical backend destination.

    The destination hash derives from backend_destination so server/Cloud URL
    aliases and mode labels pointing at the same physical endpoint resolve to one
    namespace.
    """
    q = cfg["qdrant"]
    destination = backend_destination(q)
    if q["mode"] == "embedded":
        # Existing embedded ledger rows use this two-field hash input.
        destination = (destination[0], destination[-1])
    backend = hashlib.sha256(json.dumps(destination).encode()).hexdigest()
    return q["collection"] + ":" + backend


def backend_destination(q):
    """Return a fixed (kind, scheme, hostname, port, path) destination tuple."""
    if q["mode"] == "embedded":
        return (
            "embedded",
            None,
            None,
            None,
            str(Path(q["path"]).resolve()),
        )
    endpoint = urlsplit(q["url"])
    return (
        "remote",
        endpoint.scheme.lower(),
        endpoint.hostname.lower(),
        endpoint.port or (443 if endpoint.scheme == "https" else 80),
        endpoint.path.rstrip("/"),
    )
