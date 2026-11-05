"""Fail-closed evaluation of the required CI security gate.

The gate consumes the job results that GitHub reports for the current workflow
run and the identity of the triggering event. The `scan-gate` job runs this file
from the checkout of the event under evaluation, so a pull request that edits
this file or that job can change the verdict. Review changes to both; the
accepted risk is recorded in docs/ci-security-audit.md (F-12).

Policy:

* Every job that needs no credential must succeed.
* Credentialed scans must succeed on trusted trees and must be *skipped*
  everywhere else. A successful credentialed scan in an untrusted context is
  itself a failure, because it means the token reached contributor code.
* ``skipped`` is never a pass for a job that is not explicitly listed as
  credential-gated.
"""

from __future__ import annotations

import json
import os

# Jobs that must run and succeed for every event.
ALWAYS_REQUIRED = (
    "lint",
    "tests",
    "mcp-coverage",
    "codex-windows",
    "host-pins",
    "dependency-lock",
    "workflow-security",
    "secret-scan",
)


def _result(needs: dict, job: str) -> str:
    """Return the reported result for one job, or ``missing``."""
    entry = needs.get(job)
    if not isinstance(entry, dict):
        return "missing"
    result = entry.get("result")
    return result if isinstance(result, str) and result else "missing"


def evaluate(
    needs: dict,
    *,
    event_name: str,
    fork_pr: bool,
    dependabot: bool,
    main_ref: bool,
) -> list[str]:
    """Return every policy violation for one completed workflow run."""
    problems: list[str] = []

    for job in ALWAYS_REQUIRED:
        result = _result(needs, job)
        if result != "success":
            problems.append(f"{job}={result} must succeed")

    # SonarCloud needs a maintainer token. It may run only on trees that the
    # maintainer controls: pushes, dispatches and same-repository pull requests.
    sonar = _result(needs, "sonarcloud")
    if fork_pr or dependabot:
        if sonar != "skipped":
            problems.append(f"sonarcloud={sonar} must be skipped for untrusted pull requests")
    elif sonar != "success":
        problems.append(f"sonarcloud={sonar} must succeed on trusted trees")

    # Snyk organization tokens are single-purpose and must stay off every
    # branch except the protected main tree.
    if main_ref:
        for job in ("snyk", "snyk-code"):
            result = _result(needs, job)
            if result != "success":
                problems.append(f"{job}={result} must succeed on main")
    else:
        for job in ("snyk", "snyk-code"):
            result = _result(needs, job)
            if result != "skipped":
                problems.append(f"{job}={result} must be skipped outside main")

    # Dependency review is free and credential-free, but only pull requests
    # have a base commit to compare against.
    review = _result(needs, "dependency-review")
    expected_review = "success" if event_name == "pull_request" else "skipped"
    if review != expected_review:
        problems.append(f"dependency-review={review} must be {expected_review}")

    return problems


def main(argv: list[str] | None = None) -> int:
    """Evaluate the environment that the workflow exports and exit."""
    del argv
    needs = json.loads(os.environ["NEEDS"])
    problems = evaluate(
        needs,
        event_name=os.environ.get("EVENT_NAME", ""),
        fork_pr=os.environ.get("FORK_PR") == "true",
        dependabot=os.environ.get("DEPENDABOT") == "true",
        main_ref=os.environ.get("MAIN_REF") == "true",
    )
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        print(
            "Required security gate failed. Credentialed scans are allowed to "
            "skip only where the token cannot reach contributor code."
        )
        return 1
    print("All required jobs succeeded under the configured trust policy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
