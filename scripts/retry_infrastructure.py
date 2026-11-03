"""Bounded retries for verified runner infrastructure failures only.

One controller classifies every failed job, reruns nothing when any failure is
not provably transient, and records an audit trail for each retried run.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

MAX_ATTEMPTS = 3

# Performance retries stay disabled until a real Windows run proves that the
# whitelisted durability test timed out while making durable progress. The
# diagnostics path still records evidence, but the controller fails closed.
PERFORMANCE_RETRY_ENABLED = False

ALLOWED_WORKFLOWS = frozenset(
    {
        "CI",
        "Standalone compatibility",
        "Platform compatibility",
        "Shared destination security",
    }
)

# Aggregate gates only restate the result of their prerequisites. They may be
# rerun together with an eligible root-cause job, never on their own.
GATE_JOBS = frozenset({"Required security scan gate"})

QUALITY_MARKERS = (
    "ruff",
    "lint",
    "snyk",
    "sonar",
    "codeql",
    "security",
    "dependency lock",
    "host pin",
)

INFRA_ACTION = re.compile(
    r"Run actions/(?:checkout|setup-python|upload-artifact|download-artifact)@[0-9a-f]{40}"
)

# Every pattern describes a network or GitHub service failure. Generic words
# such as "timeout" alone are deliberately absent.
TRANSIENT = re.compile(
    r"nodename nor servname|temporary failure in name resolution|"
    r"no such host is known|could not resolve host|name or service not known|"
    r"connection (?:timed out|reset|refused by peer)|operation (?:has )?timed out|"
    r"i/o timeout|tls handshake timeout|\bETIMEDOUT\b|\bECONNRESET\b|"
    r"\bHTTP(?:/\d(?:\.\d)?)?\s+(?:429|50[0234])\b|"
    r"service unavailable|bad gateway|gateway timeout|"
    r"internal server error",
    re.IGNORECASE,
)

DOWNLOAD = re.compile(r"failed to download (?:action|archive)", re.IGNORECASE)

DETERMINISTIC = re.compile(
    r"AssertionError|sqlite3\.(?:Database|Operational|Integrity)|SchemaError|"
    r"TypeError|AttributeError|WriterLease|deadlock",
    re.IGNORECASE,
)

PERFORMANCE_MARKER = re.compile(r"QDANT_WRITER_TIMEOUT (\{.*\})")
FAILED_TEST = re.compile(r"\bFAILED (tests/\S+) - ")
WHITELISTED_JOB = "local contracts ("
WHITELISTED_STEP = "Run local behavior and host lifecycle contracts"
WHITELISTED_TEST = "tests/test_provider.py::test_100_fast_turns_no_loss_and_replay"


def _failed_steps(job):
    """Return the failed steps in declaration order."""
    return [step for step in job.get("steps", []) if step.get("conclusion") == "failure"]


def _is_quality_job(name):
    """Report whether a job enforces lint, dependency, security or quality policy."""
    lowered = name.lower()
    return any(marker in lowered for marker in QUALITY_MARKERS)


def _performance_evidence(log):
    """Parse and validate the bounded writer-timeout marker, or return None."""
    match = PERFORMANCE_MARKER.search(log)
    if not match:
        return None
    try:
        report = json.loads(match.group(1))
    except (TypeError, ValueError):
        return None
    first, second = report.get("first"), report.get("second")
    if report.get("schema") != "qdrant-memory-writer-timeout-v1":
        return None
    if not isinstance(first, dict) or not isinstance(second, dict):
        return None
    if not (first.get("worker_alive") and second.get("worker_alive")):
        return None
    if not second.get("worker_busy"):
        return None
    if not report.get("progress_advanced"):
        return None
    if not report.get("no_terminal_failures"):
        return None
    if not report.get("ledger_counts_available"):
        return None
    if second.get("failed_event_count") or second.get("failed_operation_count"):
        return None
    if second.get("quarantined_event_count"):
        return None
    completed = second.get("completed_event_count", 0)
    pending = second.get("pending_event_count", 0)
    if completed + pending < 100:
        return None
    return {
        "elapsed_seconds": report.get("elapsed_seconds"),
        "completed_event_count": completed,
        "pending_event_count": pending,
        "queue_depth": second.get("queue_depth"),
        "last_progress_age_seconds": second.get("last_progress_age_seconds"),
        "worker_stack_snapshot": second.get("worker_stack_snapshot", [])[:4],
    }


def _test_failures(log):
    """Return every failed test node id reported by pytest."""
    return FAILED_TEST.findall(log)


def classify_job(job, log):
    """Classify one failed job and state the evidence behind the decision."""
    name = job.get("name") or ""
    if name in GATE_JOBS:
        return {
            "category": "DEPENDENT_GATE",
            "retryable": True,
            "gate": True,
            "basis": "Aggregate gate; rerun only together with root-cause jobs",
        }
    if _is_quality_job(name):
        return {
            "category": "NON_RETRYABLE_SECURITY",
            "retryable": False,
            "gate": False,
            "basis": "Lint, dependency, quality or security policy job",
        }
    steps = job.get("steps", [])
    failed = _failed_steps(job)
    if len(failed) != 1:
        return {
            "category": "UNKNOWN_FAILURE",
            "retryable": False,
            "gate": False,
            "basis": "Expected exactly one failed step",
        }
    step = failed[0]
    step_name = step.get("name") or ""
    index = steps.index(step)
    if any(previous.get("conclusion") == "failure" for previous in steps[:index]):
        return {
            "category": "UNKNOWN_FAILURE",
            "retryable": False,
            "gate": False,
            "basis": "An earlier step already failed",
        }
    if step_name == "Set up job":
        if len(steps) == 1 and DOWNLOAD.search(log) and TRANSIENT.search(log):
            return {
                "category": "TRANSIENT_BOOTSTRAP",
                "retryable": True,
                "gate": False,
                "basis": "Action archive download failed with a transient network error",
            }
        return {
            "category": "UNKNOWN_FAILURE",
            "retryable": False,
            "gate": False,
            "basis": "Bootstrap failure lacks verified download evidence",
        }
    earlier_steps_ok = all(
        entry.get("conclusion") in {"success", "skipped"} for entry in steps[:index]
    )
    if INFRA_ACTION.fullmatch(step_name) and earlier_steps_ok:
        if TRANSIENT.search(log) and not DETERMINISTIC.search(log):
            return {
                "category": "TRANSIENT_NETWORK",
                "retryable": True,
                "gate": False,
                "basis": "Infrastructure action failed with a transient network error",
            }
        return {
            "category": "UNKNOWN_FAILURE",
            "retryable": False,
            "gate": False,
            "basis": "Infrastructure action failed without verified transient evidence",
        }
    if WHITELISTED_JOB in name.lower() and step_name == WHITELISTED_STEP:
        failures = _test_failures(log)
        if failures and set(failures) != {WHITELISTED_TEST}:
            return {
                "category": "NON_RETRYABLE_TEST",
                "retryable": False,
                "gate": False,
                "basis": "Other test failures accompany the known durability test",
            }
        evidence = _performance_evidence(log)
        if evidence:
            if PERFORMANCE_RETRY_ENABLED:
                return {
                    "category": "TRANSIENT_PERFORMANCE",
                    "retryable": True,
                    "gate": False,
                    "basis": "Whitelisted durability test timed out while making progress",
                    "evidence": evidence,
                }
            return {
                "category": "TEST_PERFORMANCE_TIMEOUT",
                "retryable": False,
                "gate": False,
                "basis": "Diagnosed slow writer timeout; automatic retry is deferred",
                "evidence": evidence,
            }
        return {
            "category": "NON_RETRYABLE_TEST",
            "retryable": False,
            "gate": False,
            "basis": "Durability test failed without progress diagnostics",
        }
    return {
        "category": "NON_RETRYABLE_TEST",
        "retryable": False,
        "gate": False,
        "basis": "Non-infrastructure step failed",
    }


def run_problem(run, repository, *, enforce_attempt=True):
    """Return the run-level rejection reason, or None when the run is eligible."""
    if run.get("name") not in ALLOWED_WORKFLOWS:
        return "unexpected_workflow"
    if run.get("status") != "completed" or run.get("conclusion") != "failure":
        return "ineligible_run"
    if run.get("event") not in {"push", "pull_request"}:
        return "ineligible_run"
    if (run.get("head_repository") or {}).get("full_name") != repository:
        return "ineligible_run"
    if not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha") or ""):
        return "ineligible_run"
    attempt = run.get("run_attempt")
    if not isinstance(attempt, int):
        return "attempt_limit"
    if enforce_attempt and not 1 <= attempt < MAX_ATTEMPTS:
        return "attempt_limit"
    return None


def classify_run(repository, run, jobs, logs, *, enforce_attempt=True):
    """Return the complete retry decision and per-job evidence for one run."""
    problem = run_problem(run, repository, enforce_attempt=enforce_attempt)
    record = {
        "run_id": run.get("id"),
        "workflow": run.get("name"),
        "head_sha": run.get("head_sha"),
        "run_attempt": run.get("run_attempt"),
        "event": run.get("event"),
        "max_attempts": MAX_ATTEMPTS,
        "jobs": [],
    }
    if problem:
        return {**record, "decision": problem}
    failed = [job for job in jobs if job.get("conclusion") == "failure"]
    if not failed:
        return {**record, "decision": "no_failed_jobs"}
    classifications = [
        {
            "id": job.get("id"),
            "name": job.get("name"),
            **classify_job(job, logs.get(job.get("id"), "")),
        }
        for job in failed
    ]
    record["jobs"] = classifications
    substantive = [entry for entry in classifications if not entry["gate"]]
    if not substantive:
        return {**record, "decision": "gate_only_failure"}
    if any(not entry["retryable"] for entry in substantive):
        retryable_present = any(entry["retryable"] for entry in substantive)
        return {
            **record,
            "decision": "mixed_failure" if retryable_present else "not_retryable_failure",
        }
    return {**record, "decision": "retry"}


def retry_plan(repository, run, jobs, logs):
    """Return only the decision string for the supplied run metadata."""
    return classify_run(repository, run, jobs, logs)["decision"]


def _request(endpoint, *, method="GET"):
    """Use the runner's authenticated CLI without exposing log bodies or credentials."""
    command = ["gh", "api", "--method", method, endpoint]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode and "terminal escape sequences" in result.stderr:
        result = subprocess.run(
            [*command, "--allow-escape-sequences"], capture_output=True, text=True
        )
    result.check_returncode()
    return result.stdout


def _fetch_jobs(repository, run_id, attempt):
    """Read every job for one explicit attempt."""
    prefix = f"repos/{repository}/actions"
    jobs = []
    page = 1
    while True:
        batch = json.loads(
            _request(f"{prefix}/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}")
        )["jobs"]
        jobs.extend(batch)
        if len(batch) < 100:
            return jobs
        page += 1


def _fetch_logs(repository, jobs):
    """Download logs only for failed jobs; the controller never executes them."""
    prefix = f"repos/{repository}/actions"
    return {
        job["id"]: _request(f"{prefix}/jobs/{job['id']}/logs")
        for job in jobs
        if job.get("conclusion") == "failure"
    }


def _await_attempt(repository, run_id, attempt, timeout=20):
    """Wait, bounded, for the rerun to be observable before reporting success."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = json.loads(_request(f"repos/{repository}/actions/runs/{run_id}"))
        if current.get("run_attempt", 0) > attempt:
            return True
        time.sleep(2)
    return False


def process_run(repository, run_id, event_attempt=None, event_sha=None):
    """Classify the latest attempt and request only failed jobs when every failure qualifies."""
    prefix = f"repos/{repository}/actions"
    run = json.loads(_request(f"{prefix}/runs/{run_id}"))
    attempt_mismatch = event_attempt is not None and run.get("run_attempt") != event_attempt
    sha_mismatch = event_sha is not None and run.get("head_sha") != event_sha
    if attempt_mismatch or sha_mismatch:
        return {
            "run_id": run_id,
            "workflow": run.get("name"),
            "head_sha": run.get("head_sha"),
            "run_attempt": run.get("run_attempt"),
            "event_attempt": event_attempt,
            "event_sha": event_sha,
            "max_attempts": MAX_ATTEMPTS,
            "jobs": [],
            "decision": "stale_event",
        }
    record = classify_run(repository, run, [], {})
    if record["decision"] != "no_failed_jobs":
        return record
    jobs = _fetch_jobs(repository, run_id, run["run_attempt"])
    logs = _fetch_logs(repository, jobs)
    record = classify_run(repository, run, jobs, logs)
    record["run_id"] = run_id
    if record["decision"] != "retry":
        return record
    # Serialized events and a fresh metadata read prevent a stale or duplicate
    # event from rerunning a newer attempt.
    current = json.loads(_request(f"{prefix}/runs/{run_id}"))
    expected = run["run_attempt"] if event_attempt is None else event_attempt
    if current.get("run_attempt") != expected or current.get("status") != "completed":
        return {**record, "decision": "stale_event"}
    _request(f"{prefix}/runs/{run_id}/rerun-failed-jobs", method="POST")
    if not _await_attempt(repository, run_id, run["run_attempt"]):
        return {**record, "decision": "retry_unconfirmed"}
    return {**record, "decision": "retry_requested"}


def _attempt_records(repository, run_id, attempt):
    """Return bounded metadata for each attempt that already ran."""
    prefix = f"repos/{repository}/actions"
    records = []
    for number in range(1, attempt + 1):
        metadata = json.loads(_request(f"{prefix}/runs/{run_id}/attempts/{number}"))
        jobs = _fetch_jobs(repository, run_id, number)
        records.append(
            {
                "attempt": number,
                "conclusion": metadata.get("conclusion"),
                "started_at": metadata.get("run_started_at"),
                "updated_at": metadata.get("updated_at"),
                "duration_seconds": _duration_seconds(
                    metadata.get("run_started_at"), metadata.get("updated_at")
                ),
                "jobs": [
                    {
                        "id": job.get("id"),
                        "name": job.get("name"),
                        "conclusion": job.get("conclusion"),
                    }
                    for job in jobs
                ],
            }
        )
    return records


def _duration_seconds(started, finished):
    """Return a bounded attempt duration, or None when timestamps are absent."""
    try:
        start = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return round((end - start).total_seconds(), 3)


def audit_run(repository, run_id):
    """Record the final state of a run, including any earlier failure evidence."""
    prefix = f"repos/{repository}/actions"
    run = json.loads(_request(f"{prefix}/runs/{run_id}"))
    record = {
        "run_id": run_id,
        "workflow": run.get("name"),
        "head_sha": run.get("head_sha"),
        "run_attempt": run.get("run_attempt"),
        "retries_used": 0,
        "conclusion": run.get("conclusion"),
        "attempts": [],
        "first_failure": None,
        "auto_retry": False,
        "final_status": "INCONCLUSIVE",
    }
    if run.get("status") != "completed":
        return record
    attempt = run.get("run_attempt") or 1
    record["retries_used"] = max(0, attempt - 1)
    record["attempts"] = _attempt_records(repository, run_id, attempt)
    if attempt == 1 and run.get("conclusion") == "success":
        record["final_status"] = "PASS_FIRST_ATTEMPT"
        return record
    if attempt > 1 and run.get("conclusion") == "success":
        record["auto_retry"] = True
        previous = json.loads(_request(f"{prefix}/runs/{run_id}/attempts/{attempt - 1}"))
        previous = {
            **previous,
            "name": previous.get("name") or run.get("name"),
            "head_sha": previous.get("head_sha") or run.get("head_sha"),
            "event": previous.get("event") or run.get("event"),
            "head_repository": previous.get("head_repository") or run.get("head_repository"),
        }
        jobs = _fetch_jobs(repository, run_id, attempt - 1)
        logs = _fetch_logs(repository, jobs)
        first = classify_run(repository, previous, jobs, logs)
        record["first_failure"] = first
        categories = {entry["category"] for entry in first.get("jobs", []) if not entry["gate"]}
        if "TRANSIENT_PERFORMANCE" in categories:
            record["final_status"] = "PASS_AFTER_PERFORMANCE_RETRY"
        elif categories and categories <= {"TRANSIENT_BOOTSTRAP", "TRANSIENT_NETWORK"}:
            record["final_status"] = "PASS_AFTER_INFRA_RETRY"
        else:
            record["final_status"] = "PASS_AFTER_UNVERIFIED_RETRY"
        return record
    if run.get("conclusion") != "failure":
        return record
    jobs = _fetch_jobs(repository, run_id, attempt)
    logs = _fetch_logs(repository, jobs)
    current = classify_run(repository, run, jobs, logs, enforce_attempt=False)
    record["first_failure"] = current
    if current["decision"] == "retry":
        if attempt >= MAX_ATTEMPTS:
            record["final_status"] = "FAIL_RETRY_EXHAUSTED"
        else:
            record["final_status"] = "INCONCLUSIVE"
    elif current["decision"] in {"no_failed_jobs", "ineligible_run"}:
        record["final_status"] = "INCONCLUSIVE"
    else:
        record["final_status"] = "FAIL_NON_RETRYABLE"
    return record


def _summary(record):
    """Render a compact Markdown summary that preserves the original evidence."""
    lines = [
        "## CI retry audit",
        "",
        f"- Run: `{record.get('run_id')}`",
        f"- Workflow: `{record.get('workflow')}`",
        f"- Commit: `{record.get('head_sha')}`",
        f"- Attempt: `{record.get('run_attempt')}` of at most `{MAX_ATTEMPTS}`",
        f"- Decision: `{record.get('decision', record.get('final_status'))}`",
        f"- Final status: `{record.get('final_status', 'pending')}`",
        f"- Automatic retry used: `{record.get('auto_retry', False)}`",
    ]
    if record.get("jobs"):
        lines += ["", "| Job | Category | Retryable | Basis |", "| --- | --- | --- | --- |"]
        for job in record["jobs"]:
            lines.append(
                f"| {job.get('name')} | {job.get('category')} | "
                f"{job.get('retryable')} | {job.get('basis')} |"
            )
    lines += [
        "",
        "The failed attempt keeps its own logs. This record never replaces them.",
    ]
    return "\n".join(lines)


def _publish(record):
    """Print the machine-readable record and append the human summary when available."""
    print(json.dumps(record, sort_keys=True))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(_summary(record) + "\n")


def main(argv=None):
    """Consume only the GitHub-created workflow_run event and record the decision."""
    arguments = sys.argv[1:] if argv is None else argv
    repository = os.environ["GITHUB_REPOSITORY"]
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    run_id = int(event["workflow_run"]["id"])
    if "--audit" in arguments:
        _publish(audit_run(repository, run_id))
    else:
        _publish(
            process_run(
                repository,
                run_id,
                event["workflow_run"].get("run_attempt"),
                event["workflow_run"].get("head_sha"),
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
