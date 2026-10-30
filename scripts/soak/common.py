"""Shared soak-run identity, durable evidence and statistics helpers.

The soak harness keeps every observation in an append-only JSONL stream so an
Owner crash, a supervisor error or a harness exception cannot erase evidence.
Nothing here imports the Runtime, so the helpers stay usable from a plain
runner checkout and from unit tests.
"""

import json
import math
import os
import re
import secrets
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape

SCENARIOS = ("steady", "restart", "network")
PROFILES = {"quick": "quick", "full": "full"}

# The smoke window is deliberately short; the extended window is a real soak.
# A run may not report PASS for a window it did not actually cover.
PROFILE_SECONDS = {"quick": 480, "full": 18000}
FULL_ACCEPTANCE_SECONDS = 18000

RESULTS = ("PASS", "FAIL", "INCOMPLETE", "HARNESS_ERROR")

# Every window must produce all of these. A partial set is not a pass.
REQUIRED_INVARIANTS = (
    "write_durability",
    "request_id_idempotency",
    "event_id_idempotency",
    "authorization",
    "scope_isolation",
    "ledger_integrity",
    "no_unexplained_loss",
    "writer_lease",
    "harness_clean",
    "window_complete",
    "retrieval_quality",
    "workload_volume",
    "probe_results",
)

_EXIT_CODES = {"PASS": 0, "FAIL": 1, "INCOMPLETE": 2, "HARNESS_ERROR": 3}

# Token-shaped values never belong in an artifact. The harness also never puts a
# credential in a record, and this scrubber is the second line of defence.
_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s\"',}]+"),
)


def utc_now(moment=None):
    """Return an RFC3339 UTC instant for evidence correlation."""
    return (moment or datetime.now(UTC)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_run_id(scenario, profile, *, seed=None, now=None):
    """Build a sortable, unique run identifier that is safe in a collection name."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    suffix = secrets.token_hex(3) if seed is None else f"{seed & 0xFFFFFF:06x}"
    return f"soak-{profile}-{scenario}-{stamp}-{suffix}"


def collection_name(run_id):
    """Derive the Qdrant collection for one run; it carries the run identity."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", run_id.lower()).strip("_")
    return "qm_soak_" + cleaned


@dataclass(frozen=True)
class RunIdentity:
    """Describe one soak window for reports and collection naming."""

    run_id: str
    scenario: str
    profile: str
    seed: int
    commit_sha: str
    runtime_version: str
    mcp_version: str
    platform: str
    architecture: str
    started_at: str
    target_seconds: int
    source: str = "local"
    platform_label: str = ""

    def as_dict(self):
        """Return the identity as a plain mapping."""
        return asdict(self)


@dataclass
class Invariant:
    """One hard acceptance invariant with its measured evidence."""

    name: str
    status: str
    detail: str = ""
    evidence: dict = field(default_factory=dict)


class Secrets:
    """Track live credentials so every artifact writer can scrub them."""

    def __init__(self):
        """Start an empty registry."""
        self._values = []

    def register(self, value):
        """Remember a credential that must never reach an artifact."""
        if isinstance(value, str) and len(value) >= 8 and value not in self._values:
            self._values.append(value)
        return value

    def scrub(self, text):
        """Remove every registered credential and explicit credential header.

        Only registered values and explicit ``Bearer``/``api_key=`` forms are
        masked. Digests, commit SHAs and content hashes stay readable, because
        evidence that cannot be checked is not evidence.
        """
        if not isinstance(text, str):
            return text
        for value in sorted(self._values, key=len, reverse=True):
            text = text.replace(value, "<redacted>")
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(r"\1<redacted>", text)
        return text


SECRETS = Secrets()


def redact(value):
    """Scrub one string through the process-wide credential registry."""
    return SECRETS.scrub(value)


SENSITIVE_FLAGS = ("--cloud-api-key", "--cloud-llm-key")


def public_argv(argv):
    """Return a command line with credential values removed.

    The harness records its own arguments as evidence, and a flag value must
    never reach an artifact even before the credential registry is populated.
    """
    result, redact_next = [], False
    for item in argv:
        if redact_next:
            result.append("<redacted>")
            redact_next = False
            continue
        if item in SENSITIVE_FLAGS:
            result.append(item)
            redact_next = True
            continue
        for flag in SENSITIVE_FLAGS:
            if item.startswith(flag + "="):
                result.append(flag + "=<redacted>")
                break
        else:
            result.append(item)
    return result


def scrub_tree(value):
    """Recursively scrub strings inside a JSON-compatible value."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [scrub_tree(item) for item in value]
    if isinstance(value, dict):
        return {key: scrub_tree(item) for key, item in value.items()}
    return value


def write_json(path, value, *, mode=0o600, scrub=True):
    """Write JSON atomically so a truncated report never looks complete."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    document = scrub_tree(value) if scrub else value
    payload = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    with open(temporary, "w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    return path


def read_json(path):
    """Read a JSON document, returning None when it is absent or unreadable."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_jsonl(path):
    """Read an evidence stream, ignoring a torn final line from a crash."""
    rows = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


class Journal:
    """Append one durable evidence record per observation.

    Every record is flushed and fsynced. The write cost is paid per request so a
    harness or Owner crash cannot truncate the window that produced it.
    """

    def __init__(self, path, *, durable=True):
        """Open the stream in append mode and keep it open for the run."""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.durable = durable
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        self._stream = os.fdopen(descriptor, "a", encoding="utf-8")
        self.count = 0

    def record(self, kind, **fields):
        """Append one scrubbed record and return it."""
        record = {"ts": utc_now(), "mono": round(time.monotonic(), 6), "kind": kind, **fields}
        record = scrub_tree(record)
        self._stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        self._stream.flush()
        if self.durable:
            os.fsync(self._stream.fileno())
        self.count += 1
        return record

    def close(self):
        """Close the stream; the file keeps every durably written record."""
        try:
            self._stream.close()
        except OSError:  # pragma: no cover - already closed
            pass

    def rows(self):
        """Re-read the durable stream, ignoring a torn final line."""
        return read_jsonl(self.path)

    def __enter__(self):
        """Allow ``with Journal(...) as journal``."""
        return self

    def __exit__(self, *exc_info):
        """Close the stream on the way out."""
        self.close()


class Stopwatch:
    """Account for effective test time with a monotonic clock only."""

    def __init__(self, clock=time.monotonic):
        """Bind a monotonic clock and mark the start of the window."""
        self.clock = clock
        self.started = clock()
        self._stopped = None

    def elapsed(self):
        """Return effective seconds since the window started."""
        return (self._stopped or self.clock()) - self.started

    def stop(self):
        """Freeze the window and return the effective duration."""
        self._stopped = self._stopped or self.clock()
        return self.elapsed()


def percentile(values, fraction):
    """Return a nearest-rank percentile for a list of numbers."""
    ordered = sorted(value for value in values if value is not None)
    if not ordered:
        return None
    if len(ordered) == 1:
        return float(ordered[0])
    rank = max(1, min(len(ordered), math.ceil(fraction * len(ordered))))
    return float(ordered[rank - 1])


def trend(values, *, unit_seconds=60.0):
    """Describe a sampled series without inventing a fixed threshold.

    Returns the least-squares slope per minute plus the mean of the first and
    last quarters, which is robust against a single cold-start sample.
    """
    points = [(index * unit_seconds, float(value)) for index, value in enumerate(values)]
    if len(points) < 2:
        return {"samples": len(points), "slope_per_minute": 0.0, "first": None, "last": None}
    quarters = max(1, len(points) // 4)
    first = statistics.fmean(value for _, value in points[:quarters])
    last = statistics.fmean(value for _, value in points[-quarters:])
    mean_x = statistics.fmean(x for x, _ in points)
    mean_y = statistics.fmean(y for _, y in points)
    denominator = sum((x - mean_x) ** 2 for x, _ in points)
    slope = 0.0 if denominator == 0 else sum((x - mean_x) * (y - mean_y) for x, y in points)
    return {
        "samples": len(points),
        "slope_per_minute": round(slope / denominator * 60.0, 6) if denominator else 0.0,
        "first": round(first, 3),
        "last": round(last, 3),
        "change": round(last - first, 3),
    }


_WHY = {
    "PASS": "All required evidence is present and every hard invariant holds.",
    "FAIL": "At least one hard invariant failed.",
    "INCOMPLETE": "Required evidence or the required test window is missing.",
    "HARNESS_ERROR": "The harness failed; the product status is unknown.",
}


def overall_status(
    invariants,
    *,
    duration_seconds,
    target_seconds,
    harness_error=None,
    required=REQUIRED_INVARIANTS,
):
    """Derive PASS/FAIL/INCOMPLETE without ever upgrading a missing result."""
    if harness_error:
        return "HARNESS_ERROR", _WHY["HARNESS_ERROR"]
    if not target_seconds or duration_seconds is None or duration_seconds < 60:
        return "INCOMPLETE", "No accepted test window was measured."
    if duration_seconds < target_seconds * 0.95:
        return (
            "INCOMPLETE",
            f"The window covered {duration_seconds:.0f}s of a required {target_seconds}s.",
        )
    names = [item.name for item in invariants]
    missing = [name for name in required if name not in names]
    if len(names) != len(set(names)) or missing:
        return "INCOMPLETE", "The invariant set is incomplete: " + ", ".join(missing)
    if any(item.status == "FAIL" for item in invariants):
        return "FAIL", _WHY["FAIL"]
    if any(item.status in {"INCOMPLETE", "UNVERIFIED"} for item in invariants):
        return "INCOMPLETE", "At least one invariant could not be verified."
    return "PASS", _WHY["PASS"]


def exit_code(status):
    """Map a status to a distinct process exit code."""
    return _EXIT_CODES.get(status, _EXIT_CODES["INCOMPLETE"])


def write_junit(path, identity, duration_seconds, cases):
    """Write a small JUnit report so the runner records per-check results."""
    failures = sum(1 for case in cases if case["status"] != "PASS")
    tests = len(cases)
    suite = f"soak.{identity.scenario}.{identity.profile}"
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<testsuites tests="{tests}" failures="{failures}">',
        (
            f'<testsuite name="{suite}" tests="{tests}"'
            f' failures="{failures}" errors="0" skipped="0"'
            f' time="{float(duration_seconds):.3f}">'
        ),
    ]
    for case in cases:
        name = escape(str(case["name"]))
        detail = escape(str(case.get("detail", "")))
        if case["status"] == "PASS":
            lines.append(f'<testcase classname="{suite}" name="{name}"/>')
        else:
            lines.append(
                f'<testcase classname="{suite}" name="{name}">'
                f'<failure message="{escape(case["status"])}">{detail}</failure></testcase>'
            )
    lines.extend(["</testsuite>", "</testsuites>"])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return path


def free_port(host="127.0.0.1"):
    """Reserve one currently unused loopback port."""
    import socket

    with socket.socket() as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def wait_until(predicate, *, timeout, interval=0.1, clock=time.monotonic, sleep=time.sleep):
    """Poll a predicate until it is true or the deadline passes."""
    deadline = clock() + timeout
    while clock() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 - a probe failure means "not ready yet"
            pass
        sleep(interval)
    return False


def version_of(distribution, fallback="unknown"):
    """Read an installed distribution version without importing it."""
    try:
        from importlib.metadata import version

        return version(distribution)
    except Exception:  # noqa: BLE001 - a missing optional package is not fatal here
        return fallback


def platform_summary():
    """Return the operating system and CPU architecture for the report."""
    import platform
    import sys

    return f"{platform.system()} {platform.release()}", platform.machine(), sys.version.split()[0]
