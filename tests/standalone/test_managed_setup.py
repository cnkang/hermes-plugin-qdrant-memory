"""Explicit guided setup uses the existing storage/lease initialization path."""

import json
from pathlib import Path

import pytest

from qdrant_memory import standalone
from qdrant_memory.ownership import WriterLease

from .test_standalone import profile as embedding_profile


@pytest.fixture
def service(tmp_path):
    """Run a private deterministic embedding service without host dependencies."""
    yield from embedding_profile.__wrapped__(tmp_path)


def arguments(tmp_path, service, provider="ollama"):
    """Build an explicit request for a newly authorized embedded profile."""
    source, _ = service
    cfg = json.loads((source / "qdrant-memory.json").read_text())
    return [
        "setup",
        "--profile",
        str(tmp_path / "new-profile"),
        "--embedding-provider",
        provider,
        "--model",
        "fixture",
        "--dimensions",
        "3",
        "--base-url",
        cfg["embedding"]["base_url"],
        "--user-id",
        "test-user",
        "--agent-id",
        "test-agent",
    ]


@pytest.mark.parametrize("provider", ["ollama", "openai-compatible"])
def test_setup_is_idempotent_and_never_prepares_cpu_models(
    tmp_path, service, provider, monkeypatch, capsys
):
    """Preserve HTTP provider choice and stored memories across repeated setup."""
    from qdrant_memory import local_model

    def forbidden(*args, **kwargs):
        pytest.fail("HTTP embedding setup must not download a CPU model")

    monkeypatch.setattr(local_model, "prepare_model", forbidden)
    argv = arguments(tmp_path, service, provider)
    assert standalone.main(argv) == 0
    home = tmp_path / "new-profile"
    original = (home / "qdrant-memory.json").read_bytes()
    with standalone.open_runtime(home) as runtime:
        stored = standalone.call_tool(
            runtime, "qdrant_memory_add", {"text": "synthetic setup memory"}
        )
        assert stored.get("id")
    assert standalone.main(argv) == 0
    assert (home / "qdrant-memory.json").read_bytes() == original
    with standalone.open_runtime(home) as runtime:
        result = standalone.call_tool(
            runtime, "qdrant_memory_search", {"query": "synthetic setup memory"}
        )
        assert stored["id"] in str(result)
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["ready"] is True


def test_setup_refuses_unowned_or_changed_profiles(tmp_path, service, capsys):
    """Never adopt an unrelated directory or replace its contents."""
    argv = arguments(tmp_path, service)
    home = tmp_path / "new-profile"
    home.mkdir()
    sentinel = home / "existing-memory.txt"
    sentinel.write_text("must be preserved")
    assert standalone.main(argv) == 1
    assert sentinel.read_text() == "must be preserved"
    assert not (home / "qdrant-memory.json").exists()
    assert "ManagedProfileConflictError" in capsys.readouterr().out


def test_setup_does_not_change_provider_or_scope_on_repeat(tmp_path, service, capsys):
    """Refuse a changed provider instead of rewriting an owned profile."""
    argv = arguments(tmp_path, service)
    assert standalone.main(argv) == 0
    config = tmp_path / "new-profile/qdrant-memory.json"
    original = config.read_bytes()
    changed = argv.copy()
    changed[changed.index("--embedding-provider") + 1] = "openai-compatible"
    assert standalone.main(changed) == 1
    assert config.read_bytes() == original
    assert "ManagedProfileConflictError" in capsys.readouterr().out


def test_setup_keeps_writer_lease_enforcement(tmp_path, service, capsys):
    """Retain the Runtime's writer ownership check during guided setup."""
    argv = arguments(tmp_path, service)
    assert standalone.main(argv) == 0
    home = tmp_path / "new-profile"
    cfg = standalone.load_standalone_config(home)
    lease = WriterLease.for_config(home, cfg)
    try:
        assert standalone.main(argv) == 1
    finally:
        lease.close()
    assert "WriterBusyError" in capsys.readouterr().out


def test_setup_requires_explicit_embedding_choice():
    """Require a deliberate provider choice rather than silently selecting CPU."""
    parser = standalone.parser()
    argv = ["setup", "--profile", str(Path.cwd() / "profile")]
    with pytest.raises(SystemExit):
        parser.parse_args(argv)


def test_cpu_preparation_is_a_separate_explicit_cli_command(tmp_path, monkeypatch, capsys):
    """Expose preparation through the trusted CLI without adding a memory tool."""
    from qdrant_memory import local_model

    calls = []
    monkeypatch.setattr(local_model, "prepare_model", lambda cache: calls.append(cache) or tmp_path)
    assert standalone.main(["prepare-model", "--cache-dir", str(tmp_path)]) == 0
    assert calls == [tmp_path]
    assert json.loads(capsys.readouterr().out)["ready"] is True


@pytest.mark.parametrize("change", ["configuration", "invalid-configuration", "invalid-marker"])
def test_setup_rejects_changed_or_corrupt_owned_files_without_overwriting(
    tmp_path, service, capsys, change
):
    """Retry must preserve manually changed settings and corrupt ownership evidence."""
    argv = arguments(tmp_path, service)
    assert standalone.main(argv) == 0
    home = tmp_path / "new-profile"
    target = home / (".setup-owner.json" if change == "invalid-marker" else "qdrant-memory.json")
    if change == "configuration":
        value = json.loads(target.read_text())
        value["scope"]["user_id"] = "different-user"
        target.write_text(json.dumps(value))
    else:
        target.write_text("private-corrupt-configuration")
    original = target.read_bytes()
    sentinel = home / "existing-memory.txt"
    sentinel.write_text("keep this data")
    capsys.readouterr()
    assert standalone.main(argv) == 1
    assert target.read_bytes() == original
    assert sentinel.read_text() == "keep this data"
    output = capsys.readouterr().out
    assert "ManagedProfileConflictError" in output
    assert "private-corrupt-configuration" not in output


def test_cpu_setup_requires_dependencies_before_creating_or_downloading(
    tmp_path, monkeypatch, capsys
):
    """Missing optional packages leave the new profile and model cache untouched."""
    from qdrant_memory import local_embedding, local_model

    def missing():
        raise local_embedding.LocalEmbeddingUnavailableError("Install the local extra")

    def forbidden(*args, **kwargs):
        pytest.fail("Missing optional packages must not trigger a model download")

    monkeypatch.setattr(local_embedding, "require_local_dependencies", missing)
    monkeypatch.setattr(local_model, "prepare_model", forbidden)
    home = tmp_path / "new-profile"
    argv = ["setup", "--profile", str(home), "--embedding-provider", "fastembed"]
    assert standalone.main(argv) == 1
    assert not home.exists()
    assert "LocalEmbeddingUnavailableError" in capsys.readouterr().out


def test_setup_retries_a_failed_claim_with_corrected_settings(tmp_path, service, capsys):
    """A failed first attempt leaves only its marker; corrected settings may retry."""
    home = tmp_path / "new-profile"
    home.mkdir()
    (home / ".setup-owner.json").write_text(
        json.dumps({"owner": "qdrant-memory-setup", "schema": 1, "configuration_sha256": "stale"}),
        encoding="utf-8",
    )
    argv = arguments(tmp_path, service)
    assert standalone.main(argv) == 0
    assert (home / "qdrant-memory.json").exists()
    assert json.loads((home / ".setup-owner.json").read_text())["configuration_sha256"] != "stale"


def test_setup_reports_a_running_concurrent_setup(tmp_path, service, capsys):
    """A competing setup fails with a bounded conflict instead of a raw lock error."""
    import portalocker

    argv = arguments(tmp_path, service)
    lock = tmp_path / ".new-profile.setup.lock"
    with portalocker.Lock(lock, mode="a", timeout=0):
        assert standalone.main(argv) == 1
    output = capsys.readouterr().out
    assert "ManagedProfileConflictError" in output
    assert "LockException" not in output
