"""Codex portability never writes native state or opens a dry-run destination."""

import argparse
import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from qdrant_memory import codex_memory as codex
from qdrant_memory.codex_memory_cli import run
from qdrant_memory.models import Scope

requires_anchored_creation = pytest.mark.skipif(
    not codex._ANCHORED_CREATION, reason="platform lacks directory-relative creation"
)


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


@pytest.mark.parametrize(
    "secret",
    [
        "AKIAIOSFODNN7EXAMPLE",
        "ASIAY34FZKBOKMUTVV7A",
        # Keep the fixture split so the source never holds a contiguous token.
        "SLACK_TOKEN=" + "xox" + "b-EXAMPLE-TOKEN-VALUE",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload",
        "postgres://operator:s3cr3t-pass@db.example.com/app",
        "REDIS_URL=redis://:s3cr3t-pass@cache.example.com/0",
    ],
)
def test_common_credential_shapes_are_skipped(tmp_path, secret):
    """High-confidence credential formats never reach an import plan."""
    root = tmp_path / "memories"
    root.mkdir()
    (root / "MEMORY.md").write_text(f"# Credentials\nUse {secret} for the task.\n")
    records, report = codex.read_source(tmp_path)
    assert not records
    assert report["records"][0]["reason"] == "SENSITIVE_OR_CODE_REVIEW_REQUIRED"


@pytest.mark.parametrize(
    "text",
    [
        "The bearer of this note delivered the approved change.",
        "Read https://example.com/docs before the change.",
        "AKIA is only a prefix in this sentence.",
    ],
)
def test_unrelated_text_is_not_treated_as_a_credential(tmp_path, text):
    """Credential detection must not quarantine ordinary project notes."""
    root = tmp_path / "memories"
    root.mkdir()
    (root / "MEMORY.md").write_text(f"# Notes\n{text}\n")
    records, report = codex.read_source(tmp_path)
    assert len(records) == 1
    assert report["skipped"] == 0


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


@pytest.mark.parametrize("fence", ["```python", "````python", "~~~python"])
@pytest.mark.parametrize("closed", [True, False])
@pytest.mark.parametrize("separator", ["\n", "\u2028", "\x0c"])
def test_fenced_headings_never_become_importable_sections(tmp_path, fence, closed, separator):
    """Multiple code headings, including an unclosed tail, remain quarantined."""
    root = tmp_path / "memories"
    root.mkdir()
    tail = (fence.split("python")[0] + "\n# Safe after\nApproved second fact.\n") if closed else ""
    (root / "MEMORY.md").write_text(
        "# Safe before\nApproved first fact.\n# Code\nnote"
        + separator
        + fence
        + "\n# injected one\nDo not import one.\n## injected two\nDo not import two.\n"
        + tail,
        encoding="utf-8",
    )
    records, report = codex.read_source(tmp_path)
    assert len(records) == (2 if closed else 1)
    assert report["skipped"] == 1
    assert all(
        "injected" not in r["memory"] and "Do not import" not in r["memory"] for r in records
    )


def test_read_rejects_leaf_substitution_after_path_check(tmp_path, monkeypatch):
    """A symlink swapped into the final component cannot be followed at open."""
    path, outside = tmp_path / "source", tmp_path / "outside"
    path.write_text("approved")
    outside.write_text("unapproved")
    original = codex._open_nofollow

    def substitute(value):
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows account cannot create symlinks")
            raise
        return original(value)

    monkeypatch.setattr(codex, "_open_nofollow", substitute)
    with pytest.raises((OSError, ValueError)):
        codex._read(path)


def test_source_text_and_mtime_come_from_the_same_descriptor(tmp_path, monkeypatch):
    """Replacing the pathname after open cannot change provenance metadata."""
    path = tmp_path / "MEMORY.md"
    path.write_text("# Preference\nOriginal fact.\n")
    os.utime(path, (1000000000, 1000000000))
    original = codex._open_nofollow
    opened = []

    def substitute(value):
        descriptor = original(value)
        opened.append(descriptor)
        path.rename(tmp_path / "original")
        path.write_text("# Preference\nReplacement fact.\n")
        os.utime(path, (1100000000, 1100000000))
        return descriptor

    monkeypatch.setattr(
        codex,
        "inspect",
        lambda *_: {"source_id": "fixture", "stores": [{"path": path, "version": "v1"}]},
    )
    monkeypatch.setattr(codex, "_open_nofollow", substitute)
    records, _ = codex.read_source(tmp_path)
    assert len(opened) == 1
    assert records[0]["memory"].endswith("Original fact.")
    assert records[0]["updated_at"] == datetime.fromtimestamp(1000000000, UTC).isoformat()


def test_read_checks_descriptor_type_and_size_before_reading(tmp_path, monkeypatch):
    """Special files and oversized regular files never reach the stream reader."""
    path = tmp_path / "large"
    path.write_bytes(b"abc")
    with pytest.raises(ValueError, match="oversized"):
        codex._read(path, max_bytes=2)
    with pytest.raises((ValueError, OSError)):
        codex._read(tmp_path)
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        with pytest.raises(ValueError, match="Unsupported"):
            codex._read(fifo)


@requires_anchored_creation
def test_handoff_larger_than_native_source_limit_round_trips(tmp_path):
    """A valid export can exceed the independent native artifact limit."""
    scope = Scope("u", "codex")
    point = SimpleNamespace(
        id="id", payload={"text": "x" * (codex.MAX_SOURCE_BYTES + 1), **scope.as_dict()}
    )
    target = tmp_path / "large-handoff"
    codex.write_handoff(target, "test", [point], scope)
    assert codex.verify_handoff(target)["count"] == 1


@requires_anchored_creation
@pytest.mark.parametrize("filename", ["handoff.json", "handoff.md", "manifest.json"])
def test_handoff_limit_is_enforced_before_directory_publication(tmp_path, monkeypatch, filename):
    """Each file uses the same inclusive write/read bound, including UTF-8 bytes."""
    target = tmp_path / "baseline"
    scope = Scope("u", "codex")
    point = SimpleNamespace(id="id", payload={"text": '\u732b\n"' * 100, **scope.as_dict()})
    codex.write_handoff(target, "test", [point], scope)
    sizes = {p.name: p.stat().st_size for p in target.iterdir()}
    # Choose a bound below the selected file; no output directory may be created.
    monkeypatch.setattr(codex, "MAX_HANDOFF_FILE_BYTES", sizes[filename] - 1)
    refused = tmp_path / "refused"
    with pytest.raises(ValueError, match="byte limit"):
        codex.write_handoff(refused, "test", [point], scope)
    assert not refused.exists()
    with pytest.raises(ValueError, match="oversized"):
        codex.verify_handoff(target)
    monkeypatch.setattr(codex, "MAX_HANDOFF_FILE_BYTES", max(sizes.values()))
    codex.write_handoff(tmp_path / "exact-bound", "test", [point], scope)
    assert codex.verify_handoff(tmp_path / "exact-bound")["ok"]


@requires_anchored_creation
def test_handoff_creation_is_anchored_to_the_checked_parent(tmp_path, monkeypatch):
    """Swapping the verified parent cannot redirect the new bundle."""
    scope = Scope("u", "codex")
    point = SimpleNamespace(id="id", payload={"text": "Approved.", **scope.as_dict()})
    parent = tmp_path / "nested"
    parent.mkdir()
    moved = tmp_path / "moved"
    outside = tmp_path / "outside"
    outside.mkdir()
    original = codex._open_anchored

    def replace(path):
        descriptor = original(path)
        parent.rename(moved)
        parent.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(codex, "_open_anchored", replace)
    codex.write_handoff(parent / "handoff", "test", [point], scope)
    assert not list(outside.iterdir())
    assert codex.verify_handoff(moved / "handoff")["count"] == 1


@requires_anchored_creation
def test_handoff_bundle_is_created_private(tmp_path):
    """The bundle directory and its files carry creation-time private modes."""
    scope = Scope("u", "codex")
    point = SimpleNamespace(id="id", payload={"text": "Approved.", **scope.as_dict()})
    target = tmp_path / "handoff"
    codex.write_handoff(target, "test", [point], scope)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    for name in (codex.HANDOFF_JSON, codex.HANDOFF_MARKDOWN, "manifest.json"):
        assert stat.S_IMODE((target / name).stat().st_mode) == 0o600


def test_handoff_creation_refuses_platforms_without_anchoring(tmp_path, monkeypatch):
    """A platform without directory-relative creation fails closed."""
    monkeypatch.setattr(codex, "_ANCHORED_CREATION", False)
    with pytest.raises(ValueError, match="directory-relative"):
        codex.write_handoff(tmp_path / "handoff", "test", [], Scope("u", "codex"))
    assert not (tmp_path / "handoff").exists()


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


@pytest.fixture
def cli_profile(tmp_path, monkeypatch):
    """Initialize a real isolated profile with deterministic embeddings."""
    from qdrant_memory import cli, standalone

    from .helpers import Embedder, config

    class LocalEmbedder(Embedder):
        """Provide deterministic embeddings with the Runtime lifecycle contract."""

        def close(self):
            """The deterministic fixture owns no external resources."""

    home = tmp_path / "profile"
    home.mkdir()
    (home / "qdrant-memory.json").write_text(
        json.dumps(config(scope={"user_id": "fixture", "agent_id": "codex"}))
    )
    monkeypatch.setattr(cli, "build_embedder", lambda *a, **kw: LocalEmbedder())
    monkeypatch.setattr(standalone, "build_embedder", lambda *a, **kw: LocalEmbedder())
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    assert cli.run(parser.parse_args(["init", "--existing", "use"]), home=home)["ok"]
    return home


@requires_anchored_creation
def test_cli_confirmed_import_and_handoff_use_real_profile(tmp_path, monkeypatch, cli_profile):
    """Exercise CLI confirmation, scoped import and export through real storage."""
    home = cli_profile
    source = tmp_path / "native/memories_v2"
    source.mkdir(parents=True)
    (source / "memory_summary.md").write_text("# Preference\nUse verified handoffs.\n")
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(
        action="inspect",
        home=home,
        codex_home=source.parent,
        source_version=None,
        dry_run=False,
        confirm=False,
        resume=False,
        retry_failed=False,
        verify=True,
        output=None,
    )
    assert run(args)["planned"] == 1
    args.action = "migrate"
    with pytest.raises(ValueError, match="--confirm"):
        run(args)
    args.confirm = True
    imported = run(args)
    assert imported["ok"]
    assert imported["target_records"][0]["status"] == "IMPORTED"
    args.action = "export"
    args.output = tmp_path / "handoff"
    args.confirm = False
    with pytest.raises(ValueError, match="--confirm"):
        run(args)
    assert not args.output.exists()
    args.confirm = True
    assert run(args)["count"] == 1
    args.action = "verify"
    assert run(args)["count"] == 1
    args.output = None
    with pytest.raises(ValueError, match="--output"):
        run(args)


@pytest.mark.parametrize("intent", ["reset", "scope-delete"])
def test_handoff_tilde_profile_checks_actual_recovery_ledger(
    tmp_path, monkeypatch, cli_profile, intent
):
    """A quoted ~/profile must not bypass durable recovery checks."""
    from pathlib import Path

    from qdrant_memory.config import ledger_namespace
    from qdrant_memory.ledger import Ledger
    from qdrant_memory.reset import begin_destination_reset, begin_scope_delete, scope_delete_intent
    from qdrant_memory.standalone import load_standalone_config

    monkeypatch.setenv("HOME", str(cli_profile.parent))
    monkeypatch.setenv("USERPROFILE", str(cli_profile.parent))
    monkeypatch.chdir(tmp_path)
    cfg = load_standalone_config(cli_profile)
    ledger = Ledger(cli_profile, ledger_namespace(cfg))
    try:
        if intent == "reset":
            begin_destination_reset(ledger)
        else:
            begin_scope_delete(ledger, scope_delete_intent("fixture", "codex"))
    finally:
        ledger.close()
    output = tmp_path / "handoff"
    args = argparse.Namespace(action="export", home=Path("~/profile"), output=output, confirm=True)
    with pytest.raises(ValueError, match="pending recovery"):
        run(args)
    assert not output.exists()


@requires_anchored_creation
@pytest.mark.parametrize("markdown", ["# Empty handoff\n", "# Changed fact\nUnapproved memory.\n"])
def test_handoff_rejects_rehashed_markdown_with_incomplete_records(tmp_path, markdown):
    """A self-consistent byte manifest cannot hide missing or changed Markdown facts."""
    scope = Scope("fixture", "codex")
    target = tmp_path / "handoff"
    point = SimpleNamespace(id="fixture-id", payload={"text": "Approved fact.", **scope.as_dict()})
    codex.write_handoff(target, "fixture", [point], scope)
    assert codex.verify_handoff(target)["count"] == 1
    raw = markdown.encode("utf-8")
    (target / "handoff.md").write_bytes(raw)
    manifest = target / "manifest.json"
    document = json.loads(manifest.read_text())
    document["files"]["handoff.md"] = hashlib.sha256(raw).hexdigest()
    manifest.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="Markdown coverage"):
        codex.verify_handoff(target)


@pytest.mark.parametrize(
    "action,key,state,expected",
    [
        ("SKIP", None, None, "SKIPPED"),
        ("SKIP", "", None, "SKIPPED"),
        ("ADD", "op", None, "FAILED"),
        ("ADD", None, None, "FAILED"),
        ("UPDATE", "", None, "FAILED"),
        ("ADD", "op", "SUPERSEDED", "CONFLICT"),
        ("ADD", "op", "FAILED", "FAILED"),
    ],
)
def test_cli_target_status_reports_durable_outcome(action, key, state, expected):
    """Missing and superseded operations must never be reported as imported."""
    from qdrant_memory.codex_memory_cli import _target_records

    row = {"status": state} if state else None
    runtime = SimpleNamespace(ledger=SimpleNamespace(row=lambda *_: row))
    manifest = {
        "records": {"target": {"operation_key": key, "action": action, "content_hash": "hash"}}
    }
    result = _target_records(runtime, manifest)
    assert result == [{"target_id": "target", "status": expected, "content_hash": "hash"}]


@requires_anchored_creation
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
    other_scope = Scope("other", None)
    with pytest.raises(ValueError, match="scope"):
        codex.write_handoff(tmp_path / "other", "test", [point], other_scope)


@requires_anchored_creation
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


@requires_anchored_creation
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
        with pytest.raises(ConnectionError, match="simulated unavailable destination"):
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
