"""Setup fields and dashboard storage helpers."""

from pathlib import Path

from .config import DEFAULTS, merge, validate


def get_config_schema():
    """Return Hermes setup fields with credential inputs marked as secrets."""
    return [
        {
            "key": "mode",
            "description": "Qdrant deployment",
            "default": "embedded",
            "choices": ["embedded", "server", "cloud"],
        },
        {
            "key": "url",
            "description": "Qdrant server/Cloud endpoint (blank for embedded)",
            "default": "",
        },
        {
            "key": "api_key",
            "description": "Qdrant Database API key (blank for local)",
            "secret": True,
            "env_var": "QDRANT_API_KEY",
            "required": False,
        },
        {
            "key": "embedding_provider",
            "description": "Embedding provider",
            "default": "ollama",
            "choices": ["ollama", "openai-compatible"],
        },
        {
            "key": "embedding_model",
            "description": "Embedding model",
            "default": "qwen3-embedding:4b",
        },
        {
            "key": "embedding_base_url",
            "description": "Embedding endpoint",
            "default": "http://127.0.0.1:11434",
        },
        {
            "key": "embedding_dimensions",
            "description": "Embedding dimensions",
            "type": "integer",
            "default": 2560,
        },
        {
            "key": "embedding_api_key",
            "description": "Embedding API key (blank for Ollama)",
            "secret": True,
            "env_var": "EMBEDDING_API_KEY",
            "required": False,
        },
    ]


def save_config(values, hermes_home):
    """Atomically save behavior settings with mode 0600 and strip embedded secrets."""
    from utils import atomic_json_write, read_json_or_empty

    path = Path(hermes_home) / "qdrant-memory.json"
    existing = read_json_or_empty(path) if path.exists() else {}
    cfg = merge(DEFAULTS, existing)
    for key in ("mode", "url"):
        if key in values:
            cfg["qdrant"][key] = values[key] or None
    for key in ("provider", "model", "base_url", "dimensions"):
        if "embedding_" + key in values:
            cfg["embedding"][key] = values["embedding_" + key]
    # Secrets belong to Hermes's .env wizard, including keys in legacy config.
    cfg["qdrant"].pop("api_key", None)
    cfg["embedding"].pop("api_key", None)
    if "inherit_fallback" in cfg["embedding"]:
        cfg["embedding"]["inherit_fallback"].pop("api_key", None)
    validate(cfg)
    atomic_json_write(path, cfg, mode=0o600, fsync_dir=True)
    path.chmod(0o600)
