"""Codex portability never writes native state or opens a dry-run destination."""

import argparse
import json
from types import SimpleNamespace

import pytest

from qdrant_memory import codex_memory as codex
from qdrant_memory.codex_memory_cli import run
from qdrant_memory.models import Scope


@pytest.mark.parametrize(
    "version,filename", [("memories", "MEMORY.md"), ("memories_v2", "memory_summary.md")]
)
def test_supported_semantic_layouts(tmp_path, version, filename):
    """Read both verified layouts without changing source bytes."""
    root = tmp_path / version
    root.mkdir()
    source = root / filename
    source.write_text("# Preferences\n\nUse small validated changes.\n")
    before = source.read_bytes()
    records, report = codex.read_source(tmp_path)
    assert len(records) == report["planned"] == 1
    assert records[0]["memory"].endswith("changes.")
    assert source.read_bytes() == before
    assert report["native_import"] == "UNSUPPORTED"


def test_dual_write_dedup_and_exclusions(tmp_path):
    """Retain dual provenance while excluding supporting transcripts."""
    for name in ("memories", "memories_v2"):
        root = tmp_path / name
        root.mkdir()
        (root / "memory_summary.md").write_text("# Preferences\n\nUse small changes.\n")
        (root / "raw_memories.md").write_text("DO NOT IMPORT TRANSCRIPT")
    records, report = codex.read_source(tmp_path)
    assert len(records) == 1
    assert len(records[0]["codex_provenance"]["sources"]) == 2
    assert report["skipped"] == 1


def test_sensitive_code_unknown_and_empty(tmp_path):
    """Report exclusions and unknown layouts rather than importing raw data."""
    root = tmp_path / "memories"
    root.mkdir()
    (root / "unknown.json").write_text('{"secret":"ignored"}')
    assert codex.read_source(tmp_path)[1]["records"][0]["reason"] == "EMPTY_OR_UNKNOWN_LAYOUT"
    (root / "MEMORY.md").write_text("# Secret\npassword=unsafe\n# Code\n```python\nx=1\n```\n")
    records, report = codex.read_source(tmp_path)
    assert not records
    assert report["skipped"] == 2


def test_explicit_topic_filter_preserves_categories(tmp_path):
    """Select source topics without widening the fixed destination scope."""
    directory = tmp_path / "memories_v2"
    directory.mkdir()
    (directory / "memory_summary.md").write_text(
        "# Preferences\nUse tests.\n# Project\nUse Python.\n"
    )
    records, report = codex.read_source(tmp_path, headings=["Preferences"])
    assert len(records) == 1
    assert records[0]["categories"] == ["Preferences"]
    assert report["skipped"] == 1
    assert report["records"][1]["reason"] == "HEADING_FILTER"


def test_config_and_environment(tmp_path, monkeypatch):
    """Honor CODEX_HOME and reject unrecognized explicit versions."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('[memories]\nversion="v2"\nuse_memories=false\n')
    assert codex.inspect()["selected_version"] == "v2"
    assert codex.inspect()["use_memories"] is False
    (tmp_path / "config.toml").write_text('[memories]\nversion="v9"\n')
    with pytest.raises(ValueError, match="Unsupported"):
        codex.inspect()


def test_symlink_and_malformed_rejected(tmp_path):
    """Refuse substituted and corrupt native artifacts."""
    root = tmp_path / "memories"
    root.mkdir()
    (root / "MEMORY.md").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="symlink"):
        codex.read_source(tmp_path)
    (root / "MEMORY.md").unlink()
    (root / "MEMORY.md").write_bytes(b"\xff")
    with pytest.raises(UnicodeError):
        codex.read_source(tmp_path)


def test_dry_run_zero_destination_writes(tmp_path, monkeypatch):
    """Preview cannot acquire a writer or create a destination."""
    source = tmp_path / "codex"
    (source / "memories_v2").mkdir(parents=True)
    (source / "memories_v2/memory_summary.md").write_text("# Preference\nUse tests.\n")
    target = tmp_path / "never-created"
    monkeypatch.setattr("qdrant_memory.standalone.open_runtime", lambda *_: pytest.fail("opened"))
    args = argparse.Namespace(
        action="migrate", home=target, codex_home=source, source_version=None, dry_run=True
    )
    assert run(args)["planned"] == 1
    assert not target.exists()


def test_handoff_checksums_scope_no_clobber(tmp_path):
    """Detect tampering and prevent overwrites or scope widening."""
    scope = Scope("u", "codex")
    point = SimpleNamespace(id="id", payload={"text": "Use tests.", **scope.as_dict()})
    target = tmp_path / "handoff"
    assert codex.write_handoff(target, "test", [point], scope)["native_verified"] is False
    assert codex.verify_handoff(target)["count"] == 1
    with pytest.raises(FileExistsError):
        codex.write_handoff(target, "test", [point], scope)
    (target / "handoff.md").write_text("tampered")
    with pytest.raises(ValueError, match="checksum"):
        codex.verify_handoff(target)
    with pytest.raises(ValueError, match="scope"):
        codex.write_handoff(tmp_path / "other", "test", [point], Scope("other", None))


def test_manifest_path_traversal_and_false_native_success(tmp_path):
    """Manifest entries must be the fixed contained handoff filenames."""
    target = tmp_path / "handoff"
    codex.write_handoff(target, "test", [], Scope("u", None))
    path = target / "manifest.json"
    value = json.loads(path.read_text())
    value["files"]["../outside"] = "ignored"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="manifest"):
        codex.verify_handoff(target)


def test_durable_import_resume_and_export(tmp_path, monkeypatch):
    """Recover failed writes through the actual SQLite migration ledger."""
    from qdrant_memory.migration import migrate

    from .helpers import runtime

    source = tmp_path / "codex/memories_v2"
    source.mkdir(parents=True)
    (source / "memory_summary.md").write_text("# Preference\nUse small changes.\n")
    rt = runtime(tmp_path / "target")
    scope = Scope(**rt.cfg["scope"])
    records, report = codex.read_source(source.parent, scope=scope)
    original = rt.store.upsert

    def fail(_):
        raise ConnectionError("simulated unavailable destination")

    try:
        monkeypatch.setattr(rt.store, "upsert", fail)
        with pytest.raises(Exception):
            migrate(
                rt,
                records,
                "codex-native",
                report["source_id"],
                report["source_sha256"],
                verify=True,
            )
        monkeypatch.setattr(rt.store, "upsert", original)
        result = migrate(
            rt,
            records,
            "codex-native",
            report["source_id"],
            report["source_sha256"],
            resume=True,
            retry_failed=True,
            verify=True,
        )
        assert result["processed"] == 1
        assert rt.store.count(scope) == 1
        migrate(
            rt,
            records,
            "codex-native",
            report["source_id"],
            report["source_sha256"],
            resume=True,
            verify=True,
        )
        assert rt.store.count(scope) == 1
        target = tmp_path / "portable"
        codex.write_handoff(target, rt.store.collection, rt.store.scroll(scope), scope)
        assert codex.verify_handoff(target)["count"] == 1
        exported = json.loads((target / "handoff.json").read_text())
        assert exported["memories"][0]["metadata"]["codex"]["source_id"] == report["source_id"]
    finally:
        rt.store.close()
        rt.ledger.close()
