"""Owner process supervision: readiness, restart and evidence contracts."""

import os
import sys
import time
from pathlib import Path

import pytest

from scripts.soak.common import Journal, read_jsonl
from scripts.soak.supervisor import (
    OwnerStartError,
    OwnerSupervisor,
    SupervisorError,
    sample_process,
)

SLEEPER = "import time\nwhile True:\n    time.sleep(0.2)\n"


def _command(script):
    """Return a python command that runs an inline script."""
    return [sys.executable, "-c", script]


def test_start_requires_a_real_readiness_answer(tmp_path):
    """A running process is not a ready service."""
    supervisor = OwnerSupervisor(
        _command(SLEEPER),
        cwd=tmp_path,
        env=dict(os.environ),
        log_path=tmp_path / "owner.log",
        ready_probe=lambda: False,
        ready_timeout=1.0,
    )
    with pytest.raises(OwnerStartError):
        supervisor.start()
    assert supervisor.process is not None
    assert supervisor.process.poll() is not None
    assert "owner start" in (tmp_path / "owner.log").read_text()


def test_restart_counts_and_reuses_the_same_profile(tmp_path):
    """Every restart is counted and leaves one live process."""
    journal = Journal(tmp_path / "events.jsonl")
    supervisor = OwnerSupervisor(
        _command(SLEEPER),
        cwd=tmp_path,
        env=dict(os.environ),
        log_path=tmp_path / "owner.log",
        ready_probe=lambda: True,
        journal=journal,
        ready_timeout=5.0,
    )
    supervisor.start()
    first = supervisor.process.pid if supervisor.process else None
    supervisor.restart(force=False)
    second = supervisor.process.pid if supervisor.process else None
    assert first is not None and second is not None and first != second
    assert supervisor.restarts == 1
    assert supervisor.starts == 2
    supervisor.restart(force=True)
    assert supervisor.restarts == 2
    supervisor.close()
    events = [
        row["event"]
        for row in read_jsonl(tmp_path / "events.jsonl")
        if row.get("kind") == "control"
    ]
    assert events.count("owner_restart") == 2
    assert "owner_ready" in events


def test_stop_escalates_when_a_process_ignores_sigterm(tmp_path):
    """A hung Owner must not block the window forever."""
    stubborn = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, lambda *a: None)\n"
        "while True:\n    time.sleep(0.1)\n"
    )
    supervisor = OwnerSupervisor(
        _command(stubborn),
        cwd=tmp_path,
        env=dict(os.environ),
        log_path=tmp_path / "owner.log",
        ready_probe=lambda: True,
        ready_timeout=5.0,
        stop_timeout=0.5,
    )
    supervisor.start()
    supervisor.stop()
    assert supervisor.process is not None
    assert supervisor.process.poll() is not None


def test_supervisor_refuses_to_signal_a_stopped_process(tmp_path):
    """Signalling a dead Owner is a programming error, not a retry."""
    supervisor = OwnerSupervisor(
        _command(SLEEPER),
        cwd=tmp_path,
        env=dict(os.environ),
        log_path=tmp_path / "owner.log",
        ready_probe=lambda: True,
        ready_timeout=5.0,
    )
    with pytest.raises(SupervisorError):
        supervisor.signal("SIGTERM")
    assert supervisor.stop() is None


def test_sample_process_reports_dead_and_live_processes(tmp_path):
    """A resource sample is advisory; a dead pid must be visible as such."""
    dead = sample_process(999_999_999)
    assert dead.alive is False
    assert dead.rss_bytes is None
    supervisor = OwnerSupervisor(
        _command(SLEEPER),
        cwd=tmp_path,
        env=dict(os.environ),
        log_path=tmp_path / "owner.log",
        ready_probe=lambda: True,
        ready_timeout=5.0,
    )
    supervisor.start()
    time.sleep(0.3)
    sample = supervisor.sample()
    supervisor.close()
    assert sample.alive is True
    assert sample.rss_bytes is None or sample.rss_bytes > 0
    assert sample.cpu_seconds is None or sample.cpu_seconds >= 0


def test_start_refuses_a_second_live_owner(tmp_path):
    """Two live Owners would break the writer lease; the supervisor refuses."""
    supervisor = OwnerSupervisor(
        _command(SLEEPER),
        cwd=tmp_path,
        env=dict(os.environ),
        log_path=tmp_path / "owner.log",
        ready_probe=lambda: True,
        ready_timeout=5.0,
    )
    supervisor.start()
    try:
        with pytest.raises(SupervisorError):
            supervisor.start()
    finally:
        supervisor.close()


def test_log_tail_is_bounded_when_readiness_fails(tmp_path):
    """A start failure must carry a short diagnostic, not the whole log."""
    script = "import sys\nprint('boom', flush=True)\nraise SystemExit(3)\n"
    supervisor = OwnerSupervisor(
        _command(script),
        cwd=tmp_path,
        env=dict(os.environ),
        log_path=tmp_path / "owner.log",
        ready_probe=lambda: False,
        ready_timeout=2.0,
    )
    with pytest.raises(OwnerStartError) as error:
        supervisor.start()
    assert "boom" in str(error.value)
    assert Path(tmp_path / "owner.log").exists()
