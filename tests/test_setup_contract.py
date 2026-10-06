"""Run Hermes's actual schema setup wizard in an isolated directory profile."""

import json
from pathlib import Path
from types import SimpleNamespace

from hermes_cli import memory_setup
from plugins.memory import load_memory_provider
from ruamel.yaml import YAML


def test_real_memory_setup_saves_fields_secrets_and_activation(tmp_path, monkeypatch):
    """Generic setup persists behavior in JSON and credentials only in profile .env."""
    home = tmp_path / "profile"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "qdrant-memory").symlink_to(Path(__file__).resolve().parents[1])
    (home / "config.yaml").write_text("plugins:\n  isolation: in_process\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    providers = memory_setup._get_available_providers()
    assert any(name == "qdrant-memory" for name, _, _ in providers)
    selected = next(i for i, (name, _, _) in enumerate(providers) if name == "qdrant-memory")

    def choose(title, choices, **kwargs):
        """Select the real provider and OpenAI-compatible embedding mode."""
        if title == "Memory provider setup":
            return selected
        if "Embedding provider" in title:
            return 1
        return 0

    answers = {
        "Qdrant Database API key (blank for local)": "setup-qdrant-secret",
        "Embedding API key (blank for Ollama)": "setup-embedding-secret",
        "Embedding model": "custom-model",
        "Embedding endpoint": "https://example.com/v1",
        "Embedding dimensions": "3",
    }
    monkeypatch.setattr(memory_setup, "_curses_select", choose)
    monkeypatch.setattr(memory_setup, "_prompt", lambda desc, **kwargs: answers.get(desc, ""))
    monkeypatch.setattr(memory_setup, "_clear_interactive_transition", lambda: None)
    dependencies = []
    monkeypatch.setattr(memory_setup, "_install_dependencies", dependencies.append)
    memory_setup.cmd_setup(SimpleNamespace())
    saved = json.loads((home / "qdrant-memory.json").read_text())
    assert saved["embedding"]["dimensions"] == 3
    assert saved["embedding"]["model"] == "custom-model"
    assert saved["embedding"]["provider"] == "openai-compatible"
    assert saved["embedding"]["base_url"] == "https://example.com/v1"
    assert "setup-qdrant-secret" not in json.dumps(saved)
    assert "setup-embedding-secret" not in json.dumps(saved)
    secrets = (home / ".env").read_text()
    assert "QDRANT_API_KEY=setup-qdrant-secret" in secrets
    assert "EMBEDDING_API_KEY=setup-embedding-secret" in secrets
    assert (
        YAML(typ="safe").load((home / "config.yaml").read_text())["memory"]["provider"]
        == "qdrant-memory"
    )
    assert dependencies == ["qdrant-memory"]
    assert load_memory_provider("qdrant-memory").name == "qdrant-memory"
    assert not (tmp_path / "qdrant-memory.json").exists()
