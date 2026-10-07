"""Run Hermes's actual schema setup wizard in an isolated directory profile."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
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
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.delenv("QDRANT_API_KEY", raising=False)
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


@pytest.mark.parametrize("has_url,has_key", [(True, True), (True, False), (False, True)])
def test_setup_reuses_environment_without_reprompting(
    tmp_path, monkeypatch, capsys, has_url, has_key
):
    """The real host wizard skips each supplied setting and leaves credentials external."""
    from qdrant_memory.config import load_config

    home = tmp_path / "profile"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "qdrant-memory").symlink_to(Path(__file__).resolve().parents[1])
    (home / "config.yaml").write_text("plugins:\n  isolation: in_process\n")
    (home / "qdrant-memory.json").write_text(
        json.dumps({"qdrant": {"url": "https://stale.example"}})
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.delenv("QDRANT_API_KEY", raising=False)
    if has_url:
        monkeypatch.setenv("QDRANT_URL", "https://environment.example")
    if has_key:
        monkeypatch.setenv("QDRANT_API_KEY", "environment-secret-sentinel")
    providers = memory_setup._get_available_providers()
    selected = next(i for i, (name, _, _) in enumerate(providers) if name == "qdrant-memory")

    def choose(title, choices, **kwargs):
        if title == "Memory provider setup":
            return selected
        if "Qdrant deployment" in title:
            return 2
        return 0

    prompted = []

    def prompt(desc, **kwargs):
        prompted.append(desc)
        if desc.startswith("Qdrant server/Cloud endpoint"):
            assert not has_url
            return "https://manual.example"
        if desc.startswith("Qdrant Database API key"):
            assert not has_key
            return "manual-secret-sentinel"
        return str(kwargs.get("default") or "")

    monkeypatch.setattr(memory_setup, "_curses_select", choose)
    monkeypatch.setattr(memory_setup, "_prompt", prompt)
    monkeypatch.setattr(memory_setup, "_clear_interactive_transition", lambda: None)
    monkeypatch.setattr(memory_setup, "_install_dependencies", lambda name: None)
    memory_setup.cmd_setup(SimpleNamespace())
    output = capsys.readouterr().out
    assert "environment-secret-sentinel" not in output
    assert "manual-secret-sentinel" not in output
    assert any(desc.startswith("Qdrant server/Cloud endpoint") for desc in prompted) == (
        not has_url
    )
    assert any(desc.startswith("Qdrant Database API key") for desc in prompted) == (not has_key)
    saved = (home / "qdrant-memory.json").read_text()
    assert "environment-secret-sentinel" not in saved
    assert "manual-secret-sentinel" not in saved
    cfg = load_config(home)
    assert cfg["qdrant"]["url"] == (
        "https://environment.example" if has_url else "https://manual.example"
    )
    assert cfg["qdrant"]["api_key"] == (
        "environment-secret-sentinel" if has_key else "manual-secret-sentinel"
    )
    if has_url:
        assert "Using existing QDRANT_URL" in output
        assert json.loads(saved)["qdrant"]["url"] is None
    if has_key:
        assert "Using existing QDRANT_API_KEY" in output
        assert "environment-secret-sentinel" not in (
            (home / ".env").read_text() if (home / ".env").exists() else ""
        )


def test_setup_environment_lookup_respects_profile_scope(tmp_path, monkeypatch):
    """Another routed profile's global credentials cannot hide this profile's inputs."""
    from agent.secret_scope import reset_secret_scope, set_secret_scope

    from qdrant_memory.config_schema import get_config_schema, save_config

    monkeypatch.setenv("QDRANT_URL", "https://other-profile.example")
    monkeypatch.setenv("QDRANT_API_KEY", "other-profile-secret")
    monkeypatch.setattr("agent.secret_scope.serves_routed_profile", lambda: True)
    token = set_secret_scope({}, profile_home=str(tmp_path))
    try:
        assert {"url", "api_key"} <= {field["key"] for field in get_config_schema()}
    finally:
        reset_secret_scope(token)
    token = set_secret_scope(
        {"QDRANT_URL": "https://own-profile.example", "QDRANT_API_KEY": "own-secret"},
        profile_home=str(tmp_path),
    )
    try:
        assert not ({"url", "api_key"} & {field["key"] for field in get_config_schema()})
        save_config({"mode": "cloud"}, tmp_path)
        saved = (tmp_path / "qdrant-memory.json").read_text()
        assert "other-profile" not in saved and "own-secret" not in saved
    finally:
        reset_secret_scope(token)


def test_setup_rejects_invalid_environment_url_without_overwriting_config(tmp_path, monkeypatch):
    """Skipping the URL prompt does not bypass endpoint transport validation."""
    from qdrant_memory.config_schema import save_config

    monkeypatch.setenv("QDRANT_URL", "http://cloud.example")
    monkeypatch.setenv("QDRANT_API_KEY", "not-for-http")
    path = tmp_path / "qdrant-memory.json"
    original = json.dumps({"qdrant": {"mode": "embedded"}})
    path.write_text(original)
    with pytest.raises(ValueError, match="Cloud requires HTTPS"):
        save_config({"mode": "cloud"}, tmp_path)
    assert path.read_text() == original
