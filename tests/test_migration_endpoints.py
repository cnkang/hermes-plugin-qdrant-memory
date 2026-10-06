"""Distinguish collection identity across physical Qdrant destinations."""

import argparse
import json
from types import SimpleNamespace

import pytest

from qdrant_memory.cli import migration_source, register_cli
from qdrant_memory.config import load_config
from qdrant_memory.migration import source_settings


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
    client_settings = []
    client = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(
        "qdrant_memory.cli.source_client",
        lambda source_q: client_settings.append(source_q) or client,
    )
    monkeypatch.setattr(
        "qdrant_memory.cli.read_qdrant_records", lambda *args: reads.append(args[1]) or []
    )
    if allowed:
        records, kind, identifier, checksum = migration_source(args, cfg, tmp_path)
        assert records == []
        assert kind == "mem0-qdrant"
        assert identifier.startswith(cfg["qdrant"]["collection"] + ":")
        assert reads == [cfg["qdrant"]["collection"]]
        assert client_settings[0]["url"] == source_url
        assert client_settings[0]["api_key"] is None
    else:
        with pytest.raises(ValueError, match="isolated"):
            migration_source(args, cfg, tmp_path)
        assert reads == []


def test_source_endpoint_precedes_target_environment_settings(tmp_path, monkeypatch):
    """Target environment credentials must not leak to another migration source."""
    source = tmp_path / "mem0.json"
    source.write_text(
        json.dumps({"vector_store": {"config": {"host": "source.example.com", "port": 7443}}})
    )
    monkeypatch.setattr(
        "qdrant_memory.config.secret",
        lambda name: {
            "QDRANT_URL": "https://target.example.com",
            "QDRANT_API_KEY": "target-secret",
        }.get(name),
    )
    cfg = load_config(tmp_path, {"qdrant": {"mode": "cloud"}})

    source_q = source_settings(cfg, tmp_path, source)

    assert source_q["url"] == "https://source.example.com:7443"
    assert source_q["api_key"] is None


def test_matching_source_reuses_target_credentials(tmp_path):
    """Reuse target auth only when the resolved legacy endpoint is the same."""
    source = tmp_path / "mem0.json"
    source.write_text(
        json.dumps({"vector_store": {"config": {"host": "target.example.com", "port": 443}}})
    )
    cfg = load_config(
        tmp_path,
        {
            "qdrant": {
                "mode": "cloud",
                "url": "https://target.example.com",
                "api_key": "target-secret",
            }
        },
    )

    source_q = source_settings(cfg, tmp_path, source)

    assert source_q["url"] == cfg["qdrant"]["url"]
    assert source_q["api_key"] == cfg["qdrant"]["api_key"]


def test_embedded_source_path_is_not_replaced_by_target_endpoint(tmp_path):
    """An explicit embedded source path stays isolated from Cloud target config."""
    source = tmp_path / "mem0.json"
    source.write_text(json.dumps({"vector_store": {"config": {"path": "legacy-store"}}}))
    cfg = load_config(
        tmp_path,
        {
            "qdrant": {
                "mode": "cloud",
                "url": "https://target.example.com",
                "api_key": "target-secret",
            }
        },
    )

    source_q = source_settings(cfg, tmp_path, source)

    assert source_q["mode"] == "embedded"
    assert source_q["path"] == str((tmp_path / "legacy-store").resolve())
    assert source_q["url"] is None
    assert source_q["api_key"] is None


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
