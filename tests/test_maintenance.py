"""Maintenance commands and sanitized migration planning failure contracts."""

import argparse
import json
from types import SimpleNamespace

import pytest

from qdrant_memory.cli import (
    InitializationChoiceRequiredError,
    existing_collection_action,
    main,
    register_cli,
    run,
)
from qdrant_memory.migration import migrate

from .helpers import Embedder, config, runtime
from .test_provider import provider


@pytest.mark.parametrize("command", ["doctor", "stats", "verify"])
def test_missing_collection_has_actionable_error_without_creation(
    tmp_path, monkeypatch, capsys, command
):
    """Fresh installations receive an init hint while diagnostics remain read-only."""
    from qdrant_client import QdrantClient
    from utils import atomic_json_write

    atomic_json_write(tmp_path / "qdrant-memory.json", config(), mode=0o600)
    monkeypatch.setattr("qdrant_memory.cli.active_home", lambda: tmp_path)
    monkeypatch.setattr("qdrant_memory.cli.build_embedder", lambda ctx, cfg: Embedder())
    parser = argparse.ArgumentParser()
    register_cli(parser)
    assert main(parser.parse_args([command])) == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "collection_not_initialized"
    assert "hermes qdrant-memory init" in error["message"]
    assert error["retryable"] is False
    assert not (tmp_path / "qdrant-memory" / "state.db").exists()
    client = QdrantClient(path=str(tmp_path / "qdrant-memory" / "qdrant"))
    try:
        assert not client.collection_exists(config()["qdrant"]["collection"])
    finally:
        client.close()


def test_init_bootstraps_collection_and_preserves_existing_memories(tmp_path, monkeypatch):
    """Init alone prepares a usable store and repeated init preserves stored points."""
    import io

    from qdrant_client import QdrantClient, models
    from utils import atomic_json_write

    from qdrant_memory.models import Scope, payload

    atomic_json_write(tmp_path / "qdrant-memory.json", config(), mode=0o600)
    monkeypatch.setattr("qdrant_memory.cli.build_embedder", lambda ctx, cfg: Embedder())
    parser = argparse.ArgumentParser()
    register_cli(parser)
    args = parser.parse_args(["init", "--existing", "use"])
    first = run(args, home=tmp_path)
    assert first["ok"]
    assert first["embedding_probe"] == "OK"
    assert first["points_count"] == 0
    assert run(parser.parse_args(["doctor"]), home=tmp_path)["ok"]
    assert run(parser.parse_args(["verify"]), home=tmp_path)["ok"]
    client = QdrantClient(path=str(tmp_path / "qdrant-memory" / "qdrant"))
    memory = payload("keep", Scope("user", "hermes"), "manual", "session")
    try:
        client.upsert(
            config()["qdrant"]["collection"],
            points=[models.PointStruct(id=42, vector={"dense": [1.0, 0.0, 0.0]}, payload=memory)],
        )
    finally:
        client.close()
    stream = io.StringIO("\n")
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr("qdrant_memory.cli.sys.stdin", stream)
    assert run(parser.parse_args(["init"]), home=tmp_path)["action"] == "use"
    assert run(args, home=tmp_path)["points_count"] == 1
    incompatible = Embedder()
    incompatible.fingerprint = "different-pipeline"
    monkeypatch.setattr("qdrant_memory.cli.build_embedder", lambda ctx, cfg: incompatible)
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        run(args, home=tmp_path)
    client = QdrantClient(path=str(tmp_path / "qdrant-memory" / "qdrant"))
    try:
        assert client.retrieve(config()["qdrant"]["collection"], ids=[42])[0].payload == memory
    finally:
        client.close()
    monkeypatch.setattr("qdrant_memory.cli.build_embedder", lambda ctx, cfg: Embedder())
    assert run(args, home=tmp_path)["ok"]
    client = QdrantClient(path=str(tmp_path / "qdrant-memory" / "qdrant"))
    client.upsert(
        config()["qdrant"]["collection"],
        points=[models.PointStruct(id=43, vector={"dense": [1.0, 0.0, 0.0]}, payload={})],
    )
    client.close()
    invalid = run(args, home=tmp_path)
    assert not invalid["ok"]
    assert invalid["payload_verification"] == {"ok": False, "checked": 2, "invalid_count": 1}
    assert invalid["points_count"] == 2


def test_cli_does_not_expose_arbitrary_value_error_messages(monkeypatch, capsys):
    """Only the typed missing-collection error receives a fixed public message."""

    def fail(*args, **kwargs):
        raise ValueError("credential-sentinel")

    monkeypatch.setattr("qdrant_memory.cli.run", fail)
    assert main(SimpleNamespace(qdrant_command="doctor")) == 1
    output = capsys.readouterr().out
    assert "credential-sentinel" not in output
    assert json.loads(output)["error"] == {"type": "ValueError", "retryable": False}


@pytest.mark.parametrize("answer,expected", [("\n", "use"), ("use\n", "use"), ("clear\n", "clear")])
def test_existing_collection_prompt_defaults_to_use(monkeypatch, capsys, answer, expected):
    """The confirmation names the target and requires explicit input to clear."""
    import io

    stream = io.StringIO(answer)
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr("qdrant_memory.cli.sys.stdin", stream)
    assert existing_collection_action(SimpleNamespace(existing=None), "notes") == expected
    assert "'notes'" in capsys.readouterr().err


@pytest.mark.parametrize("answer,tty", [("", True), ("invalid\n", True), ("clear\n", False)])
def test_existing_collection_requires_valid_confirmation(monkeypatch, answer, tty):
    """EOF, invalid choices and noninteractive input never authorize deletion."""
    import io

    stream = io.StringIO(answer)
    monkeypatch.setattr(stream, "isatty", lambda: tty)
    monkeypatch.setattr("qdrant_memory.cli.sys.stdin", stream)
    with pytest.raises(InitializationChoiceRequiredError):
        existing_collection_action(SimpleNamespace(existing=None), "notes")


def test_clear_rebuilds_incompatible_collection_and_clears_only_target_ledger(
    tmp_path, monkeypatch
):
    """Explicit reset removes old memory and replay work while retaining other targets."""
    from qdrant_client import QdrantClient, models
    from utils import atomic_json_write

    from qdrant_memory.config import ledger_namespace, load_config
    from qdrant_memory.ledger import Ledger

    atomic_json_write(tmp_path / "qdrant-memory.json", config(), mode=0o600)
    monkeypatch.setattr("qdrant_memory.cli.build_embedder", lambda ctx, cfg: Embedder())
    cfg = load_config(tmp_path)
    client = QdrantClient(path=cfg["qdrant"]["path"])
    client.create_collection(
        cfg["qdrant"]["collection"],
        vectors_config=models.VectorParams(size=2, distance=models.Distance.DOT),
    )
    client.upsert(
        cfg["qdrant"]["collection"],
        points=[models.PointStruct(id=42, vector=[1.0, 0.0], payload={"text": "old"})],
    )
    client.close()
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    ledger.enqueue_event({"session_id": "old", "user": "old"})
    ledger.enqueue_operation("old", "UPSERT", {"text": "old"})
    ledger.set_session_unattributed("quarantined")
    ledger.close()
    other = Ledger(tmp_path, "other-destination")
    other.enqueue_operation("other", "UPSERT", {"text": "keep"})
    other.close()
    parser = argparse.ArgumentParser()
    register_cli(parser)
    with pytest.raises(ValueError, match="named dense"):
        run(parser.parse_args(["init", "--existing", "use"]), home=tmp_path)
    broken = Embedder()
    monkeypatch.setattr(broken, "embed_documents", lambda texts: [[0.0, 0.0, 0.0]])
    monkeypatch.setattr("qdrant_memory.cli.build_embedder", lambda ctx, cfg: broken)
    with pytest.raises(ValueError):
        run(parser.parse_args(["init", "--existing", "clear"]), home=tmp_path)
    client = QdrantClient(path=cfg["qdrant"]["path"])
    assert client.retrieve(cfg["qdrant"]["collection"], ids=[42])[0].payload == {"text": "old"}
    client.close()
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    assert ledger.rows("events") and ledger.rows("operations")
    ledger.close()
    monkeypatch.setattr("qdrant_memory.cli.build_embedder", lambda ctx, cfg: Embedder())
    result = run(parser.parse_args(["init", "--existing", "clear"]), home=tmp_path)
    assert result["ok"] and result["action"] == "clear"
    assert result["points_count"] == 0
    assert run(parser.parse_args(["verify"]), home=tmp_path)["ok"]
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    assert not ledger.rows("events") and not ledger.rows("operations")
    assert ledger.unattributed_sessions() == {"quarantined"}
    ledger.close()
    other = Ledger(tmp_path, "other-destination")
    assert len(other.rows("operations")) == 1
    other.close()


def test_maintenance_commands_against_real_embedded_store(tmp_path, monkeypatch):
    """Verify maintenance commands against real embedded store."""
    p = provider(tmp_path)
    p.sync_turn("cats preferred", "ack")
    assert p.wait_idle()
    p.shutdown()
    # CLI must see the same explicit configuration and private profile path.
    from utils import atomic_json_write

    atomic_json_write(tmp_path / "qdrant-memory.json", config(), mode=0o600)
    monkeypatch.setattr("qdrant_memory.cli.build_embedder", lambda ctx, cfg: Embedder())
    for command in ("status", "stats", "doctor", "verify", "retry"):
        result = run(SimpleNamespace(qdrant_command=command, collection=None), home=tmp_path)
        assert result.get("ok", True)
        if command in {"stats", "doctor", "retry"}:
            assert result["points_count"] == 1
            assert "extract_llm_latency_ms" in result["metrics"]


def test_invalid_migration_retains_sanitized_error_manifest(tmp_path):
    """Verify invalid migration retains sanitized error manifest."""
    rt = runtime(tmp_path, cfg=config(limits={"max_text_bytes": 4}))
    with pytest.raises(ValueError):
        migrate(rt, [{"id": "one", "memory": "oversized"}], "mem0-json", "fixture")
    error = rt.ledger.manifests()[-1]
    assert error["planning_error"]["type"] == "ValueError"
    assert error["failed"] == 1
    assert rt.store.count() == 0
    rt.store.close()
    rt.ledger.close()


@pytest.mark.parametrize("command", ["init", "retry"])
def test_cli_reports_held_writer_lock_before_service_access(tmp_path, monkeypatch, capsys, command):
    """An active writer blocks mutations with a useful, destination-free diagnostic."""
    from qdrant_memory.ownership import WriterLease

    cfg = config(qdrant={"mode": "cloud", "url": "https://private.example"})
    monkeypatch.setattr("qdrant_memory.cli.active_home", lambda: tmp_path)
    monkeypatch.setattr("qdrant_memory.cli.load_config", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(
        "qdrant_memory.cli.build_embedder",
        lambda *args: pytest.fail("A blocked writer must not initialize services"),
    )
    parser = argparse.ArgumentParser()
    register_cli(parser)
    args = parser.parse_args(["init", "--existing", "clear"] if command == "init" else [command])
    lease = WriterLease.for_config(tmp_path, cfg)
    try:
        assert main(args) == 1
        output = capsys.readouterr().out
        error = json.loads(output)["error"]
        assert error["code"] == "writer_busy"
        assert "Stop that runtime" in error["message"]
        assert not error["retryable"]
        assert "private.example" not in output and str(tmp_path) not in output
        assert not (tmp_path / "qdrant-memory" / "state.db").exists()
    finally:
        lease.close()
    replacement = WriterLease.for_config(tmp_path, cfg)
    replacement.close()
