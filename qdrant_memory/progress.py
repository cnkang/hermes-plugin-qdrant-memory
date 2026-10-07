"""Bounded, content-free migration diagnostics on stderr."""

import sys
import time
from threading import Event, Lock, Thread


def report(progress, stage, completed=None, total=None):
    """Notify an optional observer without including source data or credentials."""
    if progress is not None:
        try:
            progress(stage, completed, total)
        except Exception:
            # Diagnostics must not change the outcome of durable work.
            pass


class MigrationProgress:
    """Show phase changes and periodic waiting messages even during blocked I/O."""

    def __init__(self, quiet=False, interval=10.0):
        """Configure a reporter; resolve stderr at construction for CLI capture."""
        self.quiet = quiet
        self.interval = interval
        self.stream = sys.stderr
        self.started = self.changed = time.monotonic()
        self.printed = float("-inf")
        self.stage = "Starting migration"
        self.completed = self.total = None
        self.lock = Lock()
        self.stop = Event()
        self.thread = None

    def __enter__(self):
        """Start the waiting reporter before configuration or external calls."""
        self(self.stage)
        if not self.quiet:
            self.thread = Thread(target=self._heartbeat, daemon=True)
            self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        """Stop and join the reporter on success, failure, or interruption."""
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
        if exc_type is not None:
            try:
                self("Migration stopped; see error output or rerun with --resume")
            except Exception:
                # Preserve the original migration failure after reporter shutdown.
                pass

    def __call__(self, stage, completed=None, total=None):
        """Throttle record updates, but always show phase starts and completions."""
        with self.lock:
            changed = stage != self.stage
            self.stage, self.completed, self.total = stage, completed, total
            self.changed = time.monotonic()
            if (
                changed
                or self.changed - self.printed >= 1
                or (total is not None and completed == total)
            ):
                self._write()

    def _write(self, waiting=False):
        """Flush one safe line; a closed diagnostic stream must not abort writes."""
        if self.quiet:
            return
        current = time.monotonic()
        counts = "" if self.total is None else f" {self.completed}/{self.total}"
        idle = f"; waiting, no progress for {current - self.changed:.0f}s" if waiting else ""
        try:
            print(
                f"[qdrant-memory +{current - self.started:.0f}s] {self.stage}{counts}{idle}",
                file=self.stream,
                flush=True,
            )
        except (OSError, ValueError):
            self.quiet = True
        self.printed = current

    def _heartbeat(self):
        """Report inactivity without claiming that a stalled request is progressing."""
        while not self.stop.wait(self.interval):
            with self.lock:
                if time.monotonic() - self.printed >= self.interval:
                    self._write(waiting=True)
