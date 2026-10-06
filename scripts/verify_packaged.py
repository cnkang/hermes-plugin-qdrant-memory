"""Verify installed-wheel discovery through real Hermes without source-tree imports."""

import argparse
import importlib.metadata
import json
from pathlib import Path

from plugins.memory import discover_plugin_cli_commands, find_provider_dir, load_memory_provider

import qdrant_memory


def main():
    """Check entry point, package resources, setup storage and CLI discovery."""
    distribution = importlib.metadata.distribution("hermes-plugin-qdrant-memory")
    entry = next(
        ep for ep in distribution.entry_points if ep.group == "hermes_agent.memory_providers"
    )
    assert entry.name == "qdrant-memory"
    root = Path(qdrant_memory.__file__).parent
    assert "site-packages" in root.parts
    assert (root / "plugin.yaml").is_file()
    assert (root / "cli.py").is_file()
    assert (root / "config_schema.py").is_file()
    assert find_provider_dir("qdrant-memory") == root
    provider = load_memory_provider("qdrant-memory")
    assert provider is not None
    assert provider.name == "qdrant-memory"
    assert provider.get_config_schema()
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    from ruamel.yaml import YAML

    with (home / "config.yaml").open("w") as stream:
        YAML().dump(
            {"memory": {"provider": "qdrant-memory"}, "plugins": {"isolation": "in_process"}},
            stream,
        )
    provider.save_config({"embedding_dimensions": "3", "api_key": "fixture-only"}, home)
    saved = (home / "qdrant-memory.json").read_text()
    assert json.loads(saved)["embedding"]["dimensions"] == 3
    assert "fixture-only" not in saved
    commands = discover_plugin_cli_commands()
    command = next(item for item in commands if item["name"] == "qdrant-memory")
    parser = argparse.ArgumentParser()
    command["setup_fn"](parser)
    assert command["handler_fn"](parser.parse_args(["status"])) == 0
    print("PASS: installed-wheel entry point, package resources, schema persistence and CLI")


if __name__ == "__main__":
    main()
