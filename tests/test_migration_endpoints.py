"""Distinguish collection identity across physical Qdrant destinations."""

import argparse
import json
from types import SimpleNamespace

import pytest

from qdrant_memory.cli import migration_source, register_cli
from qdrant_memory.config import load_config


@pytest.mark.parametrize(
    "source_url,allowed",
    [("https://other.example.com", True), ("https://target.example.com:443/", False)],
)
def test_same_collection_requires_distinct_endpoint(tmp_path, monkeypatch, source_url, allowed):
    """Allow matching names across backends while refusing the same Cloud target."""
    source = tmp_path / "mem0.json"
    source.write_text(json.dumps({"vector_store": {"config": {"url": source_url}}}))
    cfg = load_config(
        tmp_path,
        {"qdrant": {"mode": "cloud", "url": "https://target.example.com", "api_key": "fixture"}},
    )
    parser = argparse.ArgumentParser()
    register_cli(parser)
    args = parser.parse_args(
        [
            "migrate",
            "mem0",
            "--source-qdrant-collection",
            cfg["qdrant"]["collection"],
            "--source-config",
            str(source),
        ]
    )
    reads = []
    client = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr("qdrant_memory.cli.source_client", lambda *args: client)
    monkeypatch.setattr(
        "qdrant_memory.cli.read_qdrant_records", lambda *args: reads.append(args[1]) or []
    )
    if allowed:
        records, kind, identifier, checksum = migration_source(args, cfg, tmp_path)
        assert records == []
        assert kind == "mem0-qdrant"
        assert identifier.startswith(cfg["qdrant"]["collection"] + ":")
        assert reads == [cfg["qdrant"]["collection"]]
    else:
        with pytest.raises(ValueError, match="isolated"):
            migration_source(args, cfg, tmp_path)
        assert reads == []


def test_reserved_vector_reuse_is_explicit_and_refused(tmp_path, monkeypatch, capsys):
    """The advertised reserved option cannot silently perform vector reuse."""
    from qdrant_memory.cli import main

    parser = argparse.ArgumentParser()
    register_cli(parser)
    with pytest.raises(SystemExit) as result:
        parser.parse_args(["migrate", "--help"])
    assert result.value.code == 0
    assert "Reserved: not implemented" in capsys.readouterr().out
    source = tmp_path / "records.json"
    source.write_text("[]")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    args = parser.parse_args(["migrate", "mem0", "--source-json", str(source), "--reuse-vectors"])
    assert main(args) == 1
