"""Retry only verified transient infrastructure failures, within a fixed budget."""

import json
import re
from copy import deepcopy

import pytest

from scripts import retry_infrastructure
from scripts.retry_infrastructure import audit_run, classify_run, process_run, retry_plan

REPOSITORY = "example/memory-runtime"
HEAD_SHA = "a" * 40
PERF_JOB = "Local contracts (windows-latest, Python 3.14)"
PERF_STEP = "Run local behavior and host lifecycle contracts"
PERF_TEST = "tests/test_provider.py::test_100_fast_turns_no_loss_and_replay"
BOOTSTRAP_DNS = "Failed to download action archive: nodename nor servname provided, or not known"
BOOTSTRAP_503 = "Failed to download action archive: HTTP 503 Service Unavailable"


def fixture_run(attempt=1, conclusion="failure", status="completed"):
    """Return same-repository metadata for a completed run attempt."""
    return {
        "id": 34,
        "name": "Platform compatibility",
        "status": status,
        "conclusion": conclusion,
        "run_attempt": attempt,
        "event": "pull_request",
        "head_sha": HEAD_SHA,
        "head_repository": {"full_name": REPOSITORY},
    }


def fixture_job(name=PERF_JOB, steps=None, identifier=12):
    """Return one failed job with explicit step results."""
    return {
        "id": identifier,
        "name": name,
        "conclusion": "failure",
        "steps": steps or [{"name": "Set up job", "conclusion": "failure"}],
    }


def performance_log(*, completed=57, pending=43, progress=True, alive=True, busy=True):
    """Build a pytest log with bounded writer-timeout diagnostics."""
    first = {
        "provider_state": "READY",
        "worker_alive": alive,
        "worker_busy": busy,
        "queue_depth": 44,
        "completed_event_count": completed,
        "pending_event_count": pending,
        "failed_event_count": 0,
        "failed_operation_count": 0,
        "quarantined_event_count": 0,
        "ledger_counts_available": True,
        "last_progress_age_seconds": 1.2,
        "worker_stack_snapshot": ["provider.py:123:_commit_action"],
    }
    second = deepcopy(first)
    second["completed_event_count"] = completed + (1 if progress else 0)
    second["pending_event_count"] = pending - (1 if progress else 0)
    second["queue_depth"] = 44 - (1 if progress else 0)
    report = {
        "schema": "qdrant-memory-writer-timeout-v1",
        "elapsed_seconds": 180.0,
        "sample_seconds": 2.0,
        "first": first,
        "second": second,
        "progress_advanced": progress,
        "no_terminal_failures": True,
        "ledger_counts_available": True,
    }
    return "\n".join(
        [
            f"FAILED {PERF_TEST} - assert False",
            " +  where False = wait_idle(180)",
            "QDANT_WRITER_TIMEOUT " + json.dumps(report),
            "1 failed, 1135 passed, 95 skipped, 4 warnings in 730.75s",
        ]
    )


class FakeAPI:
    """Deterministic GitHub API double; it records every request and rerun."""

    def __init__(self, run, jobs_by_attempt, logs, attempt_metadata=None):
        """Store canned metadata and fail the test on unexpected endpoints."""
        self.run = run
        self.jobs_by_attempt = jobs_by_attempt
        self.logs = logs
        self.attempt_metadata = attempt_metadata or {}
        self.calls = []
        self.posts = []
        self.log_ids = []

    def __call__(self, endpoint, *, method="GET"):
        """Answer metadata, logs and rerun requests without network access."""
        self.calls.append((endpoint, method))
        if method == "POST":
            self.posts.append(endpoint)
            self.run = {**self.run, "status": "in_progress", "conclusion": None}
            self.run["run_attempt"] = self.run.get("run_attempt", 1) + 1
            return ""
        if endpoint.endswith(f"/runs/{self.run['id']}"):
            return json.dumps(self.run)
        match = re.search(r"/attempts/(\d+)/jobs\?", endpoint)
        if match:
            return json.dumps({"jobs": self.jobs_by_attempt[int(match.group(1))]})
        match = re.fullmatch(r".*/runs/\d+/attempts/(\d+)", endpoint)
        if match:
            attempt = int(match.group(1))
            return json.dumps(self.attempt_metadata[attempt])
        match = re.fullmatch(r".*/jobs/(\d+)/logs", endpoint)
        if match:
            identifier = int(match.group(1))
            self.log_ids.append(identifier)
            return self.logs[identifier]
        raise AssertionError(f"unexpected endpoint: {endpoint}")


def bootstrap_job(identifier=12):
    """Return a job that never started a workflow step."""
    return fixture_job(identifier=identifier)


def test_confirmed_download_dns_failure_is_retryable():
    """The observed archive-download DNS failure is eligible for a bounded rerun."""
    plan = classify_run(REPOSITORY, fixture_run(), [bootstrap_job()], {12: BOOTSTRAP_DNS})
    assert plan["decision"] == "retry"
    assert plan["jobs"][0]["category"] == "TRANSIENT_BOOTSTRAP"


def test_second_attempt_may_retry_after_http_503():
    """A second transient bootstrap failure consumes the final permitted rerun."""
    plan = classify_run(REPOSITORY, fixture_run(attempt=2), [bootstrap_job()], {12: BOOTSTRAP_503})
    assert plan["decision"] == "retry"
    assert plan["run_attempt"] == 2


def test_http_status_inside_a_download_url_is_not_transient():
    """A status-like number in a URL or commit SHA cannot authorize a rerun."""
    sha = "a" * 37 + "500"
    log = (
        "Failed to download action archive: HTTP 404 Not Found from "
        f"'https://api.github.com/repos/o/r/tarball/{sha}'"
    )
    plan = classify_run(REPOSITORY, fixture_run(), [bootstrap_job()], {12: log})
    assert plan["decision"] == "not_retryable_failure"
    assert plan["jobs"][0]["category"] == "UNKNOWN_FAILURE"


def test_http_version_status_line_is_retryable():
    """An explicit HTTP status line remains valid transient evidence."""
    log = "Failed to download action archive: HTTP/1.1 502 Bad Gateway"
    assert retry_plan(REPOSITORY, fixture_run(), [bootstrap_job()], {12: log}) == "retry"


def test_third_attempt_is_never_retried():
    """Three total executions are the hard limit."""
    plan = classify_run(REPOSITORY, fixture_run(attempt=3), [bootstrap_job()], {12: BOOTSTRAP_503})
    assert plan["decision"] == "attempt_limit"


@pytest.mark.parametrize(
    "reason",
    ["test", "sqlite", "deadlock", "no_diagnostics", "wait_idle", "mixed", "manual", "fork"],
)
def test_unverified_or_non_infrastructure_failures_are_never_retried(reason):
    """No code failure, foreign head or missing evidence can consume a rerun."""
    run = fixture_run()
    jobs = [fixture_job()]
    logs = {12: performance_log()}
    steps = [{"name": PERF_STEP, "conclusion": "failure"}]
    jobs[0]["steps"] = steps
    if reason == "test":
        jobs[0]["name"] = "Behavior contracts (minimum, Python 3.11)"
        jobs[0]["steps"] = [{"name": "Run canonical host test runner", "conclusion": "failure"}]
        logs[12] = "FAILED tests/test_runtime.py::test_counts - AssertionError: 1 != 2"
    elif reason == "sqlite":
        logs[12] = "sqlite3.DatabaseError: database disk image is malformed"
    elif reason == "deadlock":
        logs[12] = performance_log(progress=False, busy=True)
    elif reason == "no_diagnostics":
        logs[12] = f"FAILED {PERF_TEST} - assert False"
    elif reason == "wait_idle":
        logs[12] = f"FAILED {PERF_TEST} - assert False\n +  where False = wait_idle(180)"
    elif reason == "mixed":
        other = fixture_job(
            name="Behavior contracts (minimum, Python 3.11)",
            steps=[{"name": "Run canonical host test runner", "conclusion": "failure"}],
            identifier=13,
        )
        jobs.append(other)
        logs[13] = "FAILED tests/test_runtime.py::test_counts - AssertionError: 1 != 2"
    elif reason == "manual":
        run["event"] = "workflow_dispatch"
    elif reason == "fork":
        run["head_repository"]["full_name"] = "foreign/fork"
    assert retry_plan(REPOSITORY, run, jobs, logs) != "retry"


def test_verified_performance_retry_stays_deferred_by_default(monkeypatch):
    """The whitelisted durability timeout records evidence but does not retry."""
    jobs = [fixture_job(steps=[{"name": PERF_STEP, "conclusion": "failure"}])]
    logs = {12: performance_log()}
    plan = classify_run(REPOSITORY, fixture_run(), jobs, logs)
    assert plan["decision"] == "not_retryable_failure"
    assert plan["jobs"][0]["category"] == "TEST_PERFORMANCE_TIMEOUT"
    assert plan["jobs"][0]["evidence"]["completed_event_count"] == 58
    monkeypatch.setattr(retry_infrastructure, "PERFORMANCE_RETRY_ENABLED", True)
    enabled = classify_run(REPOSITORY, fixture_run(), jobs, logs)
    assert enabled["decision"] == "retry"
    assert enabled["jobs"][0]["category"] == "TRANSIENT_PERFORMANCE"


def test_mixed_retryable_and_non_retryable_jobs_stop_the_retry():
    """One deterministic failure blocks the whole rerun."""
    jobs = [
        bootstrap_job(),
        fixture_job(
            name="Behavior contracts (minimum, Python 3.11)",
            steps=[{"name": "Run canonical host test runner", "conclusion": "failure"}],
            identifier=13,
        ),
    ]
    plan = classify_run(
        REPOSITORY, fixture_run(), jobs, {12: BOOTSTRAP_DNS, 13: "FAILED tests/x.py::test_y"}
    )
    assert plan["decision"] == "mixed_failure"


def test_gate_cannot_be_retried_on_its_own():
    """An aggregate gate restates prerequisites and never justifies a rerun."""
    gate = fixture_job(
        name="Required security scan gate",
        steps=[{"name": "Require successful prerequisites", "conclusion": "failure"}],
    )
    plan = classify_run(REPOSITORY, fixture_run(), [gate], {12: "connection timed out"})
    assert plan["decision"] == "gate_only_failure"
    assert plan["jobs"][0]["gate"] is True


def test_security_and_quality_jobs_are_never_retried():
    """A network-looking log cannot relax a security or quality gate."""
    jobs = [
        fixture_job(
            name="Required security scan gate",
            steps=[{"name": "Require successful prerequisites", "conclusion": "failure"}],
            identifier=13,
        ),
        fixture_job(
            name="Snyk code security",
            steps=[{"name": "Scan", "conclusion": "failure"}],
        ),
    ]
    plan = classify_run(
        REPOSITORY, fixture_run(), jobs, {12: BOOTSTRAP_DNS, 13: "connection timed out"}
    )
    assert plan["decision"] == "not_retryable_failure"
    assert plan["jobs"][1]["category"] == "NON_RETRYABLE_SECURITY"


def test_retry_budget_allows_only_two_extra_attempts():
    """Attempt two can be retried once; attempt three is final."""
    for attempt, expected in ((1, "retry"), (2, "retry"), (3, "attempt_limit")):
        assert (
            retry_plan(
                REPOSITORY, fixture_run(attempt=attempt), [bootstrap_job()], {12: BOOTSTRAP_503}
            )
            == expected
        )


def test_retry_request_targets_only_failed_jobs_and_rechecks_attempt(monkeypatch):
    """The retry request cannot replay a stale event or rerun successful jobs."""
    run = fixture_run()
    successful = {
        "id": 99,
        "name": "Local contracts (ubuntu-24.04, Python 3.14)",
        "conclusion": "success",
        "steps": [{"name": "Run tests", "conclusion": "success"}],
    }
    api = FakeAPI(run, {1: [bootstrap_job(), successful]}, {12: BOOTSTRAP_DNS})
    monkeypatch.setattr(retry_infrastructure, "_request", api)
    record = process_run(REPOSITORY, 34, event_attempt=1)
    assert record["decision"] == "retry_requested"
    assert api.posts == ["repos/example/memory-runtime/actions/runs/34/rerun-failed-jobs"]
    assert api.log_ids == [12]


def test_stale_event_does_not_request_a_second_rerun(monkeypatch):
    """A newer attempt observed before the POST cancels the request."""
    run = fixture_run()
    api = FakeAPI(run, {1: [bootstrap_job()]}, {12: BOOTSTRAP_DNS})
    original = api.__call__
    reads = 0

    def stale(endpoint, *, method="GET"):
        nonlocal reads
        if endpoint.endswith("/runs/34") and method == "GET":
            reads += 1
            if reads == 2:
                return json.dumps(fixture_run(attempt=2, status="in_progress", conclusion=""))
        return original(endpoint, method=method)

    monkeypatch.setattr(retry_infrastructure, "_request", stale)
    assert process_run(REPOSITORY, 34, event_attempt=1)["decision"] == "stale_event"
    assert api.posts == []


def test_delayed_event_for_older_attempt_is_rejected(monkeypatch):
    """An event for attempt one cannot classify or rerun attempt two."""
    run = fixture_run(attempt=2)
    api = FakeAPI(run, {2: [bootstrap_job()]}, {12: BOOTSTRAP_DNS})
    monkeypatch.setattr(retry_infrastructure, "_request", api)
    record = process_run(REPOSITORY, 34, event_attempt=1)
    assert record["decision"] == "stale_event"
    assert api.posts == []
    assert len(api.calls) == 1


def test_duplicate_event_does_not_request_a_second_rerun(monkeypatch):
    """Repeated delivery for one attempt produces at most one rerun request."""
    run = fixture_run()
    api = FakeAPI(run, {1: [bootstrap_job()]}, {12: BOOTSTRAP_DNS})
    monkeypatch.setattr(retry_infrastructure, "_request", api)
    assert process_run(REPOSITORY, 34, event_attempt=1)["decision"] == "retry_requested"
    assert process_run(REPOSITORY, 34, event_attempt=1)["decision"] != "retry_requested"
    assert len(api.posts) == 1


def test_log_download_failure_is_not_a_retry_signal(monkeypatch):
    """Missing failure evidence fails closed and never reruns a job."""
    run = fixture_run()
    jobs = [bootstrap_job()]

    def broken(endpoint, *, method="GET"):
        if endpoint.endswith("/jobs/12/logs"):
            raise RuntimeError("log download failed")
        if endpoint.endswith("/runs/34"):
            return json.dumps(run)
        if "/attempts/1/jobs?" in endpoint:
            return json.dumps({"jobs": jobs})
        raise AssertionError(endpoint)

    monkeypatch.setattr(retry_infrastructure, "_request", broken)
    with pytest.raises(RuntimeError, match="log download failed"):
        process_run(REPOSITORY, 34)


def test_github_api_error_does_not_request_a_rerun(monkeypatch):
    """An API error stops the controller instead of defaulting to retryable."""

    def broken(endpoint, *, method="GET"):
        raise RuntimeError("HTTP 500")

    monkeypatch.setattr(retry_infrastructure, "_request", broken)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        process_run(REPOSITORY, 34)


def test_repository_or_sha_mismatch_is_ineligible():
    """A stale or foreign commit identity never enters the retry path."""
    for field, value in (("head_sha", "b" * 39), ("name", "Unrelated workflow")):
        run = fixture_run()
        run[field] = value
        assert retry_plan(REPOSITORY, run, [bootstrap_job()], {12: BOOTSTRAP_DNS}) != "retry"


def test_controller_cannot_retry_itself():
    """The retry workflow is not an allowed source, so it cannot recurse."""
    run = fixture_run()
    run["name"] = "Retry transient infrastructure failures"
    assert retry_plan(REPOSITORY, run, [bootstrap_job()], {12: BOOTSTRAP_DNS}) == (
        "unexpected_workflow"
    )


def test_audit_records_first_attempt_success(monkeypatch):
    """A direct audit of a first-attempt success reports the first-attempt status."""
    run = fixture_run(conclusion="success")
    success_job = {**bootstrap_job(), "conclusion": "success"}
    api = FakeAPI(
        run,
        {1: [success_job]},
        {},
        attempt_metadata={1: run},
    )
    monkeypatch.setattr(retry_infrastructure, "_request", api)
    record = audit_run(REPOSITORY, 34)
    assert record["final_status"] == "PASS_FIRST_ATTEMPT"
    assert record["auto_retry"] is False


def test_audit_records_recovered_run_attempts_and_first_failure(monkeypatch):
    """A successful retry is reported with its original failure classification."""
    run = fixture_run(attempt=2, conclusion="success")
    attempt_one = {
        **fixture_run(attempt=1),
        "run_started_at": "2026-11-02T21:29:13Z",
        "updated_at": "2026-11-02T21:47:34Z",
    }
    attempt_two = {
        **fixture_run(attempt=2, conclusion="success"),
        "run_started_at": "2026-11-02T21:50:00Z",
        "updated_at": "2026-11-02T22:00:00Z",
    }
    success_job = {**bootstrap_job(), "conclusion": "success"}
    api = FakeAPI(
        run,
        {1: [bootstrap_job()], 2: [success_job]},
        {12: BOOTSTRAP_DNS},
        attempt_metadata={1: attempt_one, 2: attempt_two},
    )
    monkeypatch.setattr(retry_infrastructure, "_request", api)
    record = audit_run(REPOSITORY, 34)
    assert record["auto_retry"] is True
    assert record["final_status"] == "PASS_AFTER_INFRA_RETRY"
    assert record["first_failure"]["jobs"][0]["category"] == "TRANSIENT_BOOTSTRAP"
    assert record["retries_used"] == 1
    assert record["attempts"][0]["duration_seconds"] == 1101.0
    assert record["attempts"][0]["jobs"][0]["id"] == 12
    assert [entry["attempt"] for entry in record["attempts"]] == [1, 2]


def test_audit_keeps_explicit_failure_after_exhausted_retries(monkeypatch):
    """A third retryable failure stays red and is labelled as exhausted."""
    run = fixture_run(attempt=3)
    attempt_three = deepcopy(run)
    api = FakeAPI(
        run,
        {1: [bootstrap_job()], 2: [bootstrap_job()], 3: [bootstrap_job()]},
        {12: BOOTSTRAP_503},
        attempt_metadata={1: fixture_run(1), 2: fixture_run(2), 3: attempt_three},
    )
    monkeypatch.setattr(retry_infrastructure, "_request", api)
    record = audit_run(REPOSITORY, 34)
    assert record["final_status"] == "FAIL_RETRY_EXHAUSTED"
    assert record["first_failure"]["jobs"][0]["category"] == "TRANSIENT_BOOTSTRAP"


def test_audit_labels_unverified_performance_failure_non_retryable(monkeypatch):
    """The deferred durability timeout is visible and blocks the run."""
    run = fixture_run()
    jobs = [fixture_job(steps=[{"name": PERF_STEP, "conclusion": "failure"}])]
    api = FakeAPI(
        run,
        {1: jobs},
        {12: performance_log()},
        attempt_metadata={1: run},
    )
    monkeypatch.setattr(retry_infrastructure, "_request", api)
    record = audit_run(REPOSITORY, 34)
    assert record["final_status"] == "FAIL_NON_RETRYABLE"
    assert record["first_failure"]["jobs"][0]["category"] == "TEST_PERFORMANCE_TIMEOUT"
