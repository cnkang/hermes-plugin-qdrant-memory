"""Measure 100 fast turns and the writer drain on a hosted runner."""

import json
import platform
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from qdrant_memory.provider import QdrantMemoryProvider
from tests.helpers import LLM, Embedder, config

TURNS = 100
DRAIN_BUDGET = 180


def percentile(values, fraction):
    """Return a nearest-rank percentile for a non-empty list."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def main():
    """Run one isolated 100-turn probe and print one bounded JSON record."""
    home = Path(tempfile.mkdtemp(prefix="qd-writer-probe-"))
    record = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "home": str(home),
    }
    provider = QdrantMemoryProvider(
        plugin_context=SimpleNamespace(llm=LLM()),
        embedder=Embedder(),
        overrides=config(),
    )
    try:
        started = time.monotonic()
        provider.initialize("first", hermes_home=str(home))
        record["initialize_seconds"] = round(time.monotonic() - started, 3)
        record["initial_idle"] = provider.wait_idle(30)
        sync = []
        for index in range(TURNS):
            started = time.monotonic()
            provider.sync_turn(f"preference number {index}", "ack", session_id="first")
            sync.append(time.monotonic() - started)
        record["sync_total_seconds"] = round(sum(sync), 3)
        record["sync_p50_seconds"] = round(percentile(sync, 0.5), 4)
        record["sync_p95_seconds"] = round(percentile(sync, 0.95), 4)
        record["sync_max_seconds"] = round(max(sync), 4)
        started = time.monotonic()
        samples = []
        idle = False
        while time.monotonic() - started < DRAIN_BUDGET:
            if provider.wait_idle(5):
                idle = True
                break
            snapshot = provider.writer_diagnostics(time.monotonic() - started)
            samples.append(
                {
                    "elapsed_seconds": snapshot["elapsed_seconds"],
                    "completed": snapshot["completed_event_count"],
                    "pending": snapshot["pending_event_count"],
                    "failed": snapshot["failed_event_count"],
                    "queue_depth": snapshot["queue_depth"],
                    "busy": snapshot["worker_busy"],
                    "alive": snapshot["worker_alive"],
                    "last_progress_age": snapshot["last_progress_age_seconds"],
                }
            )
        record["drain_seconds"] = round(time.monotonic() - started, 3)
        record["drain_idle"] = idle
        record["drain_samples"] = samples
        if not idle:
            print(provider.timeout_report(DRAIN_BUDGET), flush=True)
        record["diagnostics"] = provider.writer_diagnostics(record["drain_seconds"])
        record["stats"] = provider.ledger.stats()
        record["metrics"] = provider.ledger.metrics()
        record["store_count"] = provider.store.count()
        try:
            provider.shutdown()
            record["shutdown"] = "ok"
        except Exception as exc:  # noqa: BLE001 - evidence must survive shutdown failures
            record["shutdown"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - one failed probe must not stop the series
        record["error"] = f"{type(exc).__name__}: {exc}"
    print("PROBE_JSON " + json.dumps(record, sort_keys=True), flush=True)
    return int(
        not (
            record.get("initial_idle")
            and record.get("drain_idle")
            and record.get("shutdown") == "ok"
            and record.get("stats", {}).get("events", {}).get("COMMITTED") == TURNS
            and record.get("store_count") == TURNS
            and record.get("diagnostics", {}).get("failed_event_count") == 0
            and record.get("diagnostics", {}).get("failed_operation_count") == 0
            and record.get("diagnostics", {}).get("pending_event_count") == 0
            and record.get("diagnostics", {}).get("pending_operation_count") == 0
            and "error" not in record
        )
    )


if __name__ == "__main__":
    sys.exit(main())
