"""Exercise live migration diagnostics without external embedding services."""

import argparse
import io
import json
from threading import Event

import pytest

from qdrant_memory import cli
from qdrant_memory.migration import migrate
from qdrant_memory.progress import MigrationProgress, report

from .helpers import Embedder, config, runtime


@pytest.mark.parametrize("quiet", [False, True])
def test_cli_migration_preserves_json_and_reports_phases(tmp_path, monkeypatch, capsys, quiet):
    """Real store writes and verification expose counts only on stderr."""
    cfg = config(qdrant={"mode": "embedded", "path": str(tmp_path / "target")})
    monkeypatch.setattr(cli, "load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr(cli, "build_embedder", lambda *a: Embedder())
    source = tmp_path / "private-export.json"
    source.write_text(json.dumps([{"id": "private-id", "memory": "private-memory"}]))
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    argv = ["migrate", "mem0", "--source-json", str(source), "--verify"]
    args = parser.parse_args(argv + (["--quiet"] if quiet else []))
    assert cli.main(args) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["processed"] == 1
    if quiet:
        assert output.err == ""
    else:
        for stage in (
            "Loading configuration",
            "Reading source",
            "Initializing target collection",
            "Comparing target and preparing manifest 1/1",
            "Embedding and writing pending records 1/1",
            "Verifying target records 1/1",
            "Migration complete 1/1",
        ):
            assert stage in output.err
        assert "private" not in output.err
    # An unchanged resume verifies settled records but writes no new batch.
    assert cli.main(parser.parse_args(argv + ["--resume"] + (["--quiet"] if quiet else []))) == 0
    resumed = capsys.readouterr()
    assert json.loads(resumed.out)["processed"] == 1
    if not quiet:
        assert "Checking resumed target records 1/1" in resumed.err
        assert "Embedding and writing pending records 0/0" in resumed.err


def test_cli_dry_run_and_source_failure_stop_reporting(tmp_path, capsys):
    """Dry runs stay local and source errors terminate with sanitized JSON."""
    source = tmp_path / "export.json"
    source.write_text(json.dumps([{"id": "one", "memory": "cats"}]))
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    argv = ["migrate", "mem0", "--source-json", str(source), "--dry-run"]
    args = parser.parse_args(argv)
    assert cli.main(args) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["dry_run"]
    assert "Dry run complete 1/1" in output.err
    assert "Connecting to target" not in output.err
    source.write_text("private-invalid-json")
    assert cli.main(args) == 1
    output = capsys.readouterr()
    assert "error" in json.loads(output.out)
    assert "Migration stopped" in output.err
    assert "Migration complete" not in output.err
    assert "private-invalid-json" not in output.out + output.err


def test_blocked_batch_reports_waiting_and_stops_thread(tmp_path, monkeypatch):
    """A blocked service call gets a heartbeat before it returns."""
    rt = runtime(tmp_path)
    waiting = Event()

    class Stream(io.StringIO):
        """Notify the test only after a waiting diagnostic is written."""

        def write(self, value):
            """Capture output and release the simulated blocked service."""
            result = super().write(value)
            if "waiting, no progress" in value:
                waiting.set()
            return result

    stream = Stream()
    original = rt.store.upsert

    def blocked(records):
        """Wait for live diagnostics while the migration call is still blocked."""
        assert waiting.wait(2), "No heartbeat during blocked write"
        original(records)

    monkeypatch.setattr(rt.store, "upsert", blocked)
    progress = MigrationProgress(interval=0.02)
    progress.stream = stream
    try:
        with progress:
            result = migrate(
                rt,
                [{"id": "one", "memory": "cats"}],
                "mem0-json",
                "fixture",
                verify=True,
                progress=progress,
            )
        assert result["processed"] == 1
        assert "Embedding and writing pending records 0/1; waiting" in stream.getvalue()
        assert not progress.thread.is_alive()
    finally:
        rt.store.close()
        rt.ledger.close()


def test_failed_batch_is_not_reported_as_committed_and_resumes(tmp_path, monkeypatch):
    """Batch counts advance only after durable commits, including retries."""
    rt = runtime(tmp_path, cfg=config(write={"batch_size": 1}))
    events = []
    original = rt.store.upsert

    def fail_second(records):
        """Fail after one committed batch with private service text."""
        if rt.store.count() == 1:
            raise ValueError("private-service-response")
        original(records)

    def observe(stage, completed=None, total=None):
        """Capture safe progress notifications for batch-boundary assertions."""
        events.append((stage, completed, total))

    records = [{"id": str(i), "memory": "cats"} for i in range(2)]
    monkeypatch.setattr(rt.store, "upsert", fail_second)
    try:
        with pytest.raises(ValueError):
            migrate(rt, records, "mem0-json", "fixture", progress=observe)
        writes = [event[1:] for event in events if event[0].startswith("Embedding")]
        assert writes == [(0, 2), (1, 2)]
        assert rt.ledger.manifests()[-1]["processed"] == 1
        monkeypatch.setattr(rt.store, "upsert", original)
        events.clear()
        result = migrate(
            rt,
            records,
            "mem0-json",
            "fixture",
            resume=True,
            retry_failed=True,
            verify=True,
            progress=observe,
        )
        assert result["processed"] == 2
        writes = [event[1:] for event in events if event[0].startswith("Embedding")]
        assert writes == [(0, 1), (1, 1)]
        assert ("Verifying target records", 2, 2) in events
    finally:
        rt.store.close()
        rt.ledger.close()


def test_reporter_throttles_and_ignores_closed_stderr(monkeypatch):
    """Record updates stay bounded and diagnostic failures do not abort migration."""
    current = [0.0]
    monkeypatch.setattr("qdrant_memory.progress.time.monotonic", lambda: current[0])
    progress = MigrationProgress()
    stream = io.StringIO()
    progress.stream = stream
    progress("Comparing target", 0, 1000)
    for i in range(1, 1000):
        progress("Comparing target", i, 1000)
    progress("Comparing target", 1000, 1000)
    assert len(stream.getvalue().splitlines()) == 2
    stream.close()
    progress("Verifying target", 0, 1000)
    assert progress.quiet


@pytest.mark.parametrize("failure", [ValueError, KeyboardInterrupt])
def test_reporter_stops_on_failure_and_quiet_never_starts_thread(failure):
    """Exceptions tear down reporting and quiet mode has no background work."""
    progress = MigrationProgress()
    progress.stream = io.StringIO()
    with pytest.raises(failure), progress:
        raise failure("private-error")
    assert not progress.thread.is_alive()
    assert "Migration stopped" in progress.stream.getvalue()
    assert "private-error" not in progress.stream.getvalue()
    with MigrationProgress(quiet=True) as quiet:
        quiet("Reading source")
    assert quiet.thread is None


@pytest.mark.parametrize("unsized", [False, True])
def test_migration_validation_progress_accepts_unsized_iterables(tmp_path, unsized):
    """A generator migrates successfully with an unknown validation total."""
    rt = runtime(tmp_path)
    records = [{"id": "one", "memory": "cats"}, {"id": "two", "memory": "dogs"}]
    events = []

    def observe(stage, completed=None, total=None):
        """Capture the validation phase's input count contract."""
        if stage == "Validating source":
            events.append((completed, total))

    try:
        result = migrate(
            rt,
            iter(records) if unsized else records,
            "mem0-json",
            "fixture",
            verify=True,
            progress=observe,
        )
        total = None if unsized else 2
        assert events == [(0, total), (1, total), (2, total)]
        assert result["processed"] == 2
        assert rt.store.count() == 2
    finally:
        rt.store.close()
        rt.ledger.close()


def test_observer_failure_after_commit_does_not_abort_migration(tmp_path):
    """A diagnostic failure after a durable batch cannot prevent later batches."""
    rt = runtime(tmp_path, cfg=config(write={"batch_size": 1}))
    committed = []

    def observe(stage, completed=None, total=None):
        """Raise only after a successful commit, verifying persisted state first."""
        if stage == "Embedding and writing pending records" and completed:
            assert rt.store.count() == completed
            committed.append(completed)
            raise RuntimeError("observer failed after commit")

    try:
        result = migrate(
            rt,
            [{"id": str(i), "memory": "cats"} for i in range(2)],
            "mem0-json",
            "fixture",
            verify=True,
            progress=observe,
        )
        assert committed == [1, 2]
        assert result["processed"] == 2
        assert rt.ledger.manifests()[-1]["completed_at"] is not None
    finally:
        rt.store.close()
        rt.ledger.close()


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit])
def test_progress_callback_preserves_base_exceptions(failure):
    """Observer isolation must not swallow process control exceptions."""
    original = failure("stop")

    def observe(*args):
        """Raise the exact control exception supplied by the test."""
        raise original

    with pytest.raises(failure) as caught:
        report(observe, "Writing", 1, 1)
    assert caught.value is original


@pytest.mark.parametrize("failure", [ValueError, KeyboardInterrupt])
def test_exit_diagnostic_failure_preserves_original_exception(monkeypatch, failure):
    """Shutdown joins the thread before isolating a failed stop message."""
    progress = MigrationProgress()
    progress.stream = io.StringIO()
    original = failure("migration failed")

    def broken_output(*args, **kwargs):
        """Simulate an unexpected output exception outside existing I/O handling."""
        raise RuntimeError("stop message failed")

    with pytest.raises(failure) as caught, progress:
        monkeypatch.setattr(progress, "_write", broken_output)
        raise original
    assert caught.value is original
    assert progress.stop.is_set()
    assert not progress.thread.is_alive()
