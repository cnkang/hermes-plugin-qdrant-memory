"""Profile-scoped configuration. Environment reads use Hermes's secret scope."""
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit
import hashlib
import json
import math

DEFAULTS = {
    "schema_version": 1,
    "scope": {"user_id": "hermes-user", "agent_id": "hermes"},
    "qdrant": {"mode": "embedded", "collection": "hermes_qdrant_memory", "url": None,
               "api_key": None, "url_env": "QDRANT_URL", "api_key_env": "QDRANT_API_KEY",
               "timeout_seconds": 5, "prefer_grpc": False},
    "llm": {"mode": "inherit", "task": "qdrant_memory_extraction"},
    "embedding": {"mode": "plugin", "provider": "ollama", "model": "qwen3-embedding:4b",
                  "base_url": "http://127.0.0.1:11434", "dimensions": 2560,
                  "distance": "Cosine", "batch_size": 32, "send_dimensions": False,
                  "api_key_env": "EMBEDDING_API_KEY", "timeout_seconds": 30},
    "search": {"mode": "dense", "top_k": 8, "candidate_k": 24, "min_score": None,
               "hnsw_ef": 128, "exact": False, "rerank": False},
    "dedupe": {"similarity_review_threshold": 0.92, "high_similarity_threshold": 0.97,
               "time_decay_half_life_days": 365},
    "write": {"batch_size": 64, "max_attempts": 5, "backoff_base_seconds": 0.5,
              "backoff_max_seconds": 30, "shutdown_timeout_seconds": 5},
    "limits": {"max_text_bytes": 65536, "max_metadata_bytes": 32768,
               "max_payload_bytes": 131072, "oversize_policy": "reject"},
}


def merge(base, values):
    result = deepcopy(base)
    for key, value in values.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def secret(name):
    from agent.secret_scope import get_secret
    return get_secret(name)


def active_home():
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def validate_url(value, cloud=False):
    parsed = urlsplit(value or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Endpoint must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Credentials and query parameters are forbidden in endpoint URLs")
    if cloud and parsed.scheme != "https":
        raise ValueError("Cloud requires HTTPS")


def validate(cfg):
    if cfg["schema_version"] != 1:
        raise ValueError("Unsupported configuration schema")
    if cfg["qdrant"]["mode"] not in {"embedded", "server", "cloud"}:
        raise ValueError("Invalid Qdrant mode")
    if not isinstance(cfg["qdrant"]["collection"], str) or not cfg["qdrant"]["collection"]:
        raise ValueError("Collection is required")
    if cfg["llm"]["mode"] not in {"inherit", "task", "override"}:
        raise ValueError("Invalid LLM mode")
    if cfg["llm"]["mode"] == "task" and cfg["llm"]["task"] != "qdrant_memory_extraction":
        raise ValueError("Use the plugin's registered auxiliary task")
    if cfg["llm"]["mode"] == "override" and not all(cfg["llm"].get(k) for k in ("provider", "model")):
        raise ValueError("LLM override requires provider and model")
    if cfg["embedding"]["mode"] not in {"plugin", "inherit"}:
        raise ValueError("Invalid embedding mode")
    if cfg["embedding"]["mode"] == "inherit" and not cfg["embedding"].get("inherit_fallback"):
        raise ValueError("Embedding inheritance requires an explicit fallback")
    emb = embedding_config(cfg)
    if emb["provider"] not in {"ollama", "openai-compatible"}:
        raise ValueError("Unsupported embedding provider")
    validate_url(emb["base_url"])
    if emb["distance"] not in {"Cosine", "Dot", "Euclid"}:
        raise ValueError("Unsupported distance metric")
    positive = [(emb, "dimensions"), (emb, "batch_size"), (emb, "timeout_seconds"),
                (cfg["search"], "top_k"), (cfg["search"], "candidate_k"),
                (cfg["search"], "hnsw_ef"), (cfg["write"], "batch_size"),
                (cfg["write"], "max_attempts"), (cfg["qdrant"], "timeout_seconds")]
    positive += [(cfg["limits"], k) for k in ("max_text_bytes", "max_metadata_bytes", "max_payload_bytes")]
    for group, key in positive:
        if not isinstance(group[key], (int, float)) or isinstance(group[key], bool) or not math.isfinite(group[key]) or group[key] <= 0:
            raise ValueError(f"{key} must be positive")
        if key not in {"timeout_seconds"} and not isinstance(group[key], int):
            raise ValueError(f"{key} must be an integer")
    for key in ("backoff_base_seconds", "backoff_max_seconds", "shutdown_timeout_seconds"):
        value = cfg["write"][key]
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{key} must be nonnegative and finite")
    for key in ("similarity_review_threshold", "high_similarity_threshold"):
        if not 0 <= cfg["dedupe"][key] <= 1:
            raise ValueError("Similarity threshold must be between zero and one")
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
    path = Path(home) / "qdrant-memory.json"
    from utils import read_json_or_empty
    values = read_json_or_empty(path) if path.exists() else {}
    if not isinstance(values, dict):
        raise ValueError("Plugin configuration must be an object")
    cfg = merge(merge(DEFAULTS, values), overrides or {})
    validate(cfg)
    q = cfg["qdrant"]
    q["path"] = str(Path(q.get("path") or Path(home) / "qdrant-memory" / "qdrant").expanduser())
    if resolve_secrets:
        q["url"] = q.get("url") or secret(q["url_env"])
        q["api_key"] = q.get("api_key") or secret(q["api_key_env"])
        if q["mode"] != "embedded":
            q["url"] = q["url"] or ("http://127.0.0.1:6333" if q["mode"] == "server" else None)
            validate_url(q["url"], cloud=q["mode"] == "cloud")
            if q["mode"] == "cloud" and not q["api_key"]:
                raise ValueError("Cloud requires a scoped QDRANT_API_KEY")
    return cfg


def embedding_config(cfg):
    emb = cfg["embedding"]
    return merge(DEFAULTS["embedding"], emb.get("inherit_fallback", {})) if emb["mode"] == "inherit" else emb


def fingerprint(emb):
    keys = ("provider", "model", "dimensions", "distance", "document_instruction",
            "query_instruction", "normalization_version", "base_url", "send_dimensions")
    return hashlib.sha256(json.dumps({k: emb.get(k) for k in keys}, sort_keys=True).encode()).hexdigest()


def ledger_namespace(cfg):
    q = cfg["qdrant"]
    endpoint = str(Path(q["path"]).resolve()) if q["mode"] == "embedded" else q["url"].rstrip("/")
    backend = hashlib.sha256(json.dumps([q["mode"], endpoint]).encode()).hexdigest()
    return q["collection"] + ":" + backend
