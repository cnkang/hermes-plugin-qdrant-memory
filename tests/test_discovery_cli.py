import argparse
import json
import logging
from pathlib import Path
from plugins.memory import load_memory_provider, discover_plugin_cli_commands, find_provider_dir
from qdrant_memory.cli import register_cli, run
from qdrant_memory.extraction import Extractor
from .helpers import LLM


def test_real_directory_discovery_and_cli(tmp_path, monkeypatch, caplog):
    home = tmp_path / "home"
    (home / "plugins").mkdir(parents=True)
    root = Path(__file__).resolve().parents[1]
    (home / "plugins" / "qdrant-memory").symlink_to(root, target_is_directory=True)
    (home / "config.yaml").write_text("memory:\n  provider: qdrant-memory\nplugins:\n  isolation: in_process\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert find_provider_dir("qdrant-memory") == home / "plugins" / "qdrant-memory"
    with caplog.at_level(logging.DEBUG):
        provider = load_memory_provider("qdrant-memory")
    assert provider is not None and provider.name == "qdrant-memory", caplog.text
    assert provider.context.llm is not None
    assert {s["name"] for s in provider.get_tool_schemas()} == {
        "qdrant_memory_search", "qdrant_memory_add", "qdrant_memory_update", "qdrant_memory_delete"}
    commands = discover_plugin_cli_commands()
    assert any(c["name"] == "qdrant-memory" for c in commands)
    command = next(c for c in commands if c["name"] == "qdrant-memory")
    parser = argparse.ArgumentParser()
    command["setup_fn"](parser)
    assert command["handler_fn"](parser.parse_args(["status"])) == 0


def test_dry_run_has_no_target_or_ledger_writes(tmp_path):
    source = tmp_path / "export.json"
    source.write_text(json.dumps([{"id": "one", "memory": "cats preferred"}]))
    parser = argparse.ArgumentParser()
    register_cli(parser)
    args = parser.parse_args(["migrate", "mem0", "--source-json", str(source), "--dry-run"])
    result = run(args, home=tmp_path / "home")
    assert result["source_unique_count"] == 1
    assert not (tmp_path / "home").exists()


def test_llm_inherit_and_task_routing():
    llm = LLM()
    event = {"user": "cats", "assistant": "ack"}
    Extractor(llm, {"mode": "inherit"}).extract(event)
    assert not any(k in llm.calls[-1] for k in ("task", "provider", "model"))
    Extractor(llm, {"mode": "task", "task": "qdrant_memory_extraction"}).extract(event)
    assert llm.calls[-1]["task"] == "qdrant_memory_extraction"
