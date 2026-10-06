from types import SimpleNamespace
import pytest
from qdrant_memory.cli import run
from qdrant_memory.migration import migrate
from .helpers import Embedder, config, runtime
from .test_provider import provider


def test_maintenance_commands_against_real_embedded_store(tmp_path, monkeypatch):
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
    rt = runtime(tmp_path, cfg=config(limits={"max_text_bytes": 4}))
    with pytest.raises(ValueError):
        migrate(rt, [{"id": "one", "memory": "oversized"}], "mem0-json", "fixture")
    error = rt.ledger.manifests()[-1]
    assert error["planning_error"]["type"] == "ValueError"
    assert error["failed"] == 1
    assert rt.store.count() == 0
    rt.store.close()
    rt.ledger.close()
