"""Supervise one native Owner process across controlled restarts.

The soak needs a real process it can stop gracefully, kill without warning and
start again on the same profile, port and ledger. This module owns exactly that
lifecycle and reports every transition as evidence.
"""

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


class OwnerStartError(RuntimeError):
    """The Owner process did not reach readiness."""


class SupervisorError(RuntimeError):
    """The supervisor was used in an unsupported state."""


@dataclass
class ProcessSample:
    """One resource sample for the supervised process."""

    rss_bytes: float | None
    file_descriptors: int | None
    cpu_seconds: float | None
    alive: bool

    def as_dict(self):
        """Return the sample as a plain mapping."""
        return {
            "rss_bytes": self.rss_bytes,
            "file_descriptors": self.file_descriptors,
            "cpu_seconds": self.cpu_seconds,
            "alive": self.alive,
        }


def _linux_sample(pid):
    """Read RSS, descriptor count and CPU time from procfs."""
    rss = None
    cpu = None
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss = float(line.split()[1]) * 1024.0
                break
    except OSError:
        return None, None, None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        ticks = float(fields[11]) + float(fields[12])
        cpu = ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError):
        cpu = None
    try:
        descriptors = len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        descriptors = None
    return rss, descriptors, cpu


def _macos_sample(pid):
    """Read RSS and CPU time with ps, and descriptors with lsof."""
    rss = cpu = descriptors = None
    try:
        result = subprocess.run(
            ["ps", "-o", "rss=", "-o", "time=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None, None
    parts = result.stdout.split()
    if len(parts) >= 2:
        try:
            rss = float(parts[0]) * 1024.0
        except ValueError:
            rss = None
        try:
            seconds = 0.0
            for chunk in parts[1].split(":"):
                seconds = seconds * 60 + float(chunk)
            cpu = seconds
        except ValueError:
            cpu = None
    try:
        listing = subprocess.run(
            ["lsof", "-p", str(pid)], capture_output=True, text=True, timeout=15
        )
        descriptors = (
            max(0, len(listing.stdout.splitlines()) - 1) if listing.returncode == 0 else None
        )
    except (OSError, subprocess.SubprocessError):
        descriptors = None
    return rss, descriptors, cpu


def sample_process(pid):
    """Return one best-effort resource sample for a live process."""
    if not pid:
        return ProcessSample(None, None, None, alive=False)
    try:
        os.kill(pid, 0)
    except OSError:
        return ProcessSample(None, None, None, alive=False)
    if os.path.exists("/proc/self/status"):
        rss, descriptors, cpu = _linux_sample(pid)
    else:
        rss, descriptors, cpu = _macos_sample(pid)
    return ProcessSample(rss, descriptors, cpu, alive=True)


class OwnerSupervisor:
    """Start, probe, stop and restart one Owner process, recording evidence."""

    def __init__(
        self,
        command,
        *,
        cwd,
        env,
        log_path,
        ready_probe,
        journal=None,
        stop_timeout=30.0,
        ready_timeout=90.0,
    ):
        """Bind the launch contract; nothing starts until ``start``."""
        self.command = list(command)
        self.cwd = Path(cwd)
        self.env = dict(env)
        self.log_path = Path(log_path)
        self.ready_probe = ready_probe
        self.journal = journal
        self.stop_timeout = stop_timeout
        self.ready_timeout = ready_timeout
        self.process = None
        self.restarts = 0
        self.downtime_seconds = 0.0
        self.starts = 0

    def _record(self, event, **fields):
        """Publish one supervisor transition when a journal is attached."""
        if self.journal is not None:
            self.journal.record("control", event=event, **fields)

    def _log_tail(self, limit=40):
        """Return the last lines of the Owner log for a bounded diagnostic."""
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return lines[-limit:]

    def start(self):
        """Start the Owner and wait for a real authenticated readiness answer."""
        if self.process is not None and self.process.poll() is None:
            raise SupervisorError("The Owner is already running")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        with open(self.log_path, "a", encoding="utf-8") as log:
            log.write(
                f"\n=== owner start {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ===\n"
            )
            log.flush()
            process = subprocess.Popen(
                self.command,
                cwd=str(self.cwd),
                env=self.env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.process = process
        self.starts += 1
        ready = self._wait_ready()
        elapsed = time.monotonic() - started
        if not ready:
            tail = self._log_tail()
            self.stop(force=True)
            self._record("owner_start_failed", seconds=round(elapsed, 3), log_tail=tail)
            raise OwnerStartError(
                "Owner did not reach readiness within "
                f"{self.ready_timeout:.0f}s: " + " | ".join(tail[-6:])
            )
        self._record("owner_ready", seconds=round(elapsed, 3), pid=process.pid)
        return {"pid": process.pid, "ready_seconds": round(elapsed, 3)}

    def _wait_ready(self):
        """Poll the authenticated probe until it answers or the process exits."""
        process = self.process
        if process is None:
            return False
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return False
            try:
                if self.ready_probe():
                    return True
            except Exception:  # noqa: BLE001 - a failed probe means "not ready"
                pass
            time.sleep(0.2)
        return False

    def alive(self):
        """Return whether the Owner process is still running."""
        return self.process is not None and self.process.poll() is None

    def _live_process(self):
        """Return the running process or raise a supervisor error."""
        process = self.process
        if process is None or process.poll() is not None:
            raise SupervisorError("The Owner is not running")
        return process

    def signal(self, name):
        """Send one signal to the Owner without waiting for it.

        Windows has no ``SIGKILL``. There, both a graceful and a forced stop end
        in ``TerminateProcess``, which is the platform's forced termination.
        """
        process = self._live_process()
        if name == "SIGKILL" and not hasattr(signal, "SIGKILL"):
            process.kill()
            return process.pid
        os.kill(process.pid, getattr(signal, name))
        return process.pid

    def stop(self, *, force=False, timeout=None):
        """Stop the Owner, escalating to SIGKILL when the deadline passes."""
        process = self.process
        if process is None:
            return None
        if process.poll() is not None:
            process.wait()
            return process.returncode
        started = time.monotonic()
        signalled = "SIGKILL" if force else "SIGTERM"
        try:
            self.signal(signalled)
        except SupervisorError:
            return process.poll()
        deadline = started + (timeout if timeout is not None else self.stop_timeout)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.05)
        escalated = False
        if process.poll() is None:
            escalated = True
            try:
                self.signal("SIGKILL")
            except (OSError, SupervisorError):
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - unkillable process
                pass
        self.downtime_seconds += time.monotonic() - started
        self._record(
            "owner_stop",
            signal=signalled,
            escalated=escalated,
            exit_code=process.returncode,
            seconds=round(time.monotonic() - started, 3),
        )
        return process.returncode

    def restart(self, *, force=False):
        """Stop the Owner and start it again on the same profile."""
        self.stop(force=force)
        self.restarts += 1
        self._record("owner_restart", force=force, restarts=self.restarts)
        result = self.start()
        return {"restart": self.restarts, "force": force, **result}

    def sample(self):
        """Return one resource sample for the running Owner."""
        process = self.process
        pid = process.pid if process is not None and process.poll() is None else None
        return sample_process(pid)

    def close(self):
        """Stop the Owner if it is still running."""
        if self.alive():
            self.stop()
        return self.restarts
