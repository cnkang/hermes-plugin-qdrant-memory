"""Bounded retries for verified runner bootstrap download failures only."""

import json
import os
import re
import subprocess
from pathlib import Path

MAX_ATTEMPTS = 3
TRANSIENT = re.compile(
    r"nodename nor servname|temporary failure in name resolution|"
    r"no such host is known|could not resolve host|name or service not known|"
    r"connection (?:timed out|reset)|operation (?:has )?timed out|"
    r"HTTP[^\n]*(?:429|50[0234])|service unavailable|bad gateway|gateway timeout",
    re.IGNORECASE,
)
DOWNLOAD = re.compile(r"failed to download (?:action|archive)", re.IGNORECASE)


def retry_plan(repository, run, jobs, logs):
    """Require same-repository completion, remaining budget and evidence for every failure."""
    if (
        run.get("status") != "completed"
        or run.get("conclusion") != "failure"
        or run.get("event") not in {"push", "pull_request"}
        or (run.get("head_repository") or {}).get("full_name") != repository
    ):
        return "ineligible_run"
    if not 1 <= run.get("run_attempt", MAX_ATTEMPTS) < MAX_ATTEMPTS:
        return "attempt_limit"
    failed = [job for job in jobs if job.get("conclusion") == "failure"]
    if not failed:
        return "no_failed_jobs"
    for job in failed:
        steps = job.get("steps", [])
        if (
            len(steps) != 1
            or steps[0].get("name") != "Set up job"
            or steps[0].get("conclusion") != "failure"
        ):
            return "not_bootstrap_failure"
        log = logs.get(job["id"], "")
        if not DOWNLOAD.search(log) or not TRANSIENT.search(log):
            return "unverified_download_failure"
    return "retry"


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


def process_run(repository, run_id):
    """Re-read live metadata before requesting a failed-jobs-only rerun."""
    prefix = f"repos/{repository}/actions"
    run = json.loads(_request(f"{prefix}/runs/{run_id}"))
    # Avoid downloading logs for foreign heads, stale events or exhausted runs.
    preliminary = retry_plan(repository, run, [], {})
    if preliminary != "no_failed_jobs":
        return preliminary
    jobs = []
    page = 1
    while True:
        batch = json.loads(
            _request(
                f"{prefix}/runs/{run_id}/attempts/{run['run_attempt']}/jobs?per_page=100&page={page}"
            )
        )["jobs"]
        jobs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    logs = {
        job["id"]: _request(f"{prefix}/jobs/{job['id']}/logs")
        for job in jobs
        if job.get("conclusion") == "failure"
    }
    decision = retry_plan(repository, run, jobs, logs)
    if decision == "retry":
        # The workflow serializes events per run. A final metadata read also
        # prevents stale events from rerunning a newer in-progress attempt.
        current = json.loads(_request(f"{prefix}/runs/{run_id}"))
        if current.get("run_attempt") != run["run_attempt"] or current.get("status") != "completed":
            return "stale_event"
        _request(f"{prefix}/runs/{run_id}/rerun-failed-jobs", method="POST")
    return decision


def main():
    """Consume only the GitHub-created workflow_run event and print a sanitized decision."""
    repository = os.environ["GITHUB_REPOSITORY"]
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    run_id = int(event["workflow_run"]["id"])
    result = process_run(repository, run_id)
    print(json.dumps({"run_id": run_id, "decision": result, "max_attempts": MAX_ATTEMPTS}))


if __name__ == "__main__":
    main()
