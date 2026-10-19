"""Retry only confirmed transient action downloads, within a fixed attempt budget."""

from copy import deepcopy

import pytest

from scripts.retry_infrastructure import retry_plan

REPOSITORY = "example/memory-runtime"


def fixture_run():
    """Return same-repository metadata for a completed initial failure."""
    return {
        "status": "completed",
        "conclusion": "failure",
        "run_attempt": 1,
        "event": "pull_request",
        "head_repository": {"full_name": REPOSITORY},
    }


def fixture_job():
    """Represent a runner failure before any workflow step executes."""
    return {
        "id": 12,
        "conclusion": "failure",
        "steps": [{"name": "Set up job", "conclusion": "failure"}],
    }


def test_confirmed_download_dns_failure_is_retryable():
    """The observed archive-download DNS failure is eligible for a bounded rerun."""
    logs = {12: "Failed to download action archive: nodename nor servname provided, or not known"}
    assert retry_plan(REPOSITORY, fixture_run(), [fixture_job()], logs) == "retry"


@pytest.mark.parametrize(
    "reason",
    [
        "budget",
        "fork",
        "running",
        "cancelled",
        "test",
        "quality",
        "missing",
        "permanent",
        "mixed",
        "manual",
    ],
)
def test_unconfirmed_or_non_infrastructure_failures_are_never_retried(reason):
    """No code failure, foreign head or missing evidence can consume a rerun."""
    run, job = fixture_run(), fixture_job()
    jobs = [job]
    logs = {12: "Failed to download action: connection timed out"}
    if reason == "budget":
        run["run_attempt"] = 3
    elif reason == "fork":
        run["head_repository"]["full_name"] = "foreign/fork"
    elif reason == "running":
        run["status"] = "in_progress"
    elif reason == "cancelled":
        run["conclusion"] = "cancelled"
    elif reason in {"test", "quality"}:
        job["steps"] = [
            {"name": "Set up job", "conclusion": "success"},
            {"name": "Run tests" if reason == "test" else "Quality gate", "conclusion": "failure"},
        ]
    elif reason == "missing":
        logs = {}
    elif reason == "permanent":
        logs[12] = "Failed to download action archive: HTTP 404 Not Found"
    elif reason == "mixed":
        other = deepcopy(job)
        other["id"] = 13
        other["steps"] = [{"name": "Run tests", "conclusion": "failure"}]
        jobs.append(other)
    elif reason == "manual":
        run["event"] = "workflow_dispatch"
    assert retry_plan(REPOSITORY, run, jobs, logs) != "retry"


def test_retry_budget_allows_only_two_extra_attempts():
    """Attempt two can be retried once; attempt three is final."""
    run = fixture_run()
    run["run_attempt"] = 2
    logs = {12: "Failed to download archive: HTTP 503 Service Unavailable"}
    assert retry_plan(REPOSITORY, run, [fixture_job()], logs) == "retry"
    run["run_attempt"] = 3
    assert retry_plan(REPOSITORY, run, [fixture_job()], logs) != "retry"


@pytest.mark.parametrize("stale", [False, True])
def test_retry_request_targets_only_failed_jobs_and_rechecks_attempt(monkeypatch, stale):
    """The retry action cannot replay a stale event or rerun successful jobs."""
    import json

    from scripts import retry_infrastructure

    calls = []
    reads = 0

    def request(endpoint, *, method="GET"):
        nonlocal reads
        calls.append((endpoint, method))
        if endpoint.endswith("/runs/34"):
            reads += 1
            run = fixture_run()
            if stale and reads == 2:
                run["status"] = "in_progress"
            return json.dumps(run)
        if "/attempts/1/jobs?" in endpoint:
            return json.dumps({"jobs": [fixture_job()]})
        if endpoint.endswith("/jobs/12/logs"):
            return "Failed to download action: connection timed out"
        assert endpoint.endswith("/rerun-failed-jobs")
        assert method == "POST"
        return ""

    monkeypatch.setattr(retry_infrastructure, "_request", request)
    assert retry_infrastructure.process_run(REPOSITORY, 34) == ("stale_event" if stale else "retry")
    assert sum(method == "POST" for _, method in calls) == (0 if stale else 1)
