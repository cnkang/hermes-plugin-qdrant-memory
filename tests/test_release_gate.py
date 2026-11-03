"""The release gate must fail closed unless every required workflow passed."""

import pytest

from scripts.check_release_gate import REQUIRED_WORKFLOWS, check_release_gate

REPOSITORY = "example/memory-runtime"
SHA = "a" * 40


def run(
    name,
    *,
    conclusion="success",
    status="completed",
    branch="main",
    event="push",
    sha=SHA,
    identifier=1,
):
    """Build one workflow run record with explicit identity fields."""
    return {
        "id": identifier,
        "name": name,
        "conclusion": conclusion,
        "status": status,
        "head_branch": branch,
        "event": event,
        "head_sha": sha,
        "html_url": f"https://example.test/runs/{identifier}",
    }


def all_successful():
    """Return one successful main-push run per required workflow."""
    return [run(name, identifier=index) for index, name in enumerate(REQUIRED_WORKFLOWS, 1)]


def test_release_gate_passes_for_all_required_workflows():
    """A commit with four successful main-push runs passes."""
    report = check_release_gate(REPOSITORY, SHA, fetch=lambda *_: all_successful())
    assert report["passed"] is True
    assert {entry["status"] for entry in report["workflows"].values()} == {"success"}


@pytest.mark.parametrize("status", ["missing", "failed", "incomplete"])
def test_release_gate_fails_closed(status):
    """A missing, failed or in-progress workflow blocks publication."""
    runs = all_successful()
    target = REQUIRED_WORKFLOWS[0]
    runs = [entry for entry in runs if entry["name"] != target]
    if status == "failed":
        runs.append(run(target, conclusion="failure"))
    elif status == "incomplete":
        runs.append(run(target, status="in_progress", conclusion=""))
    report = check_release_gate(REPOSITORY, SHA, fetch=lambda *_: runs)
    assert report["passed"] is False
    assert report["workflows"][target]["status"] == status


def test_pull_request_and_branch_runs_do_not_satisfy_the_gate():
    """Only a main-branch push proves the merge commit's matrix."""
    runs = all_successful() + [
        run(REQUIRED_WORKFLOWS[0], event="pull_request", branch="feature"),
        run(REQUIRED_WORKFLOWS[1], branch="codex/experiment"),
    ]
    report = check_release_gate(REPOSITORY, SHA, fetch=lambda *_: runs)
    assert report["passed"] is True  # the main-push runs still exist
    runs = [run(name, event="pull_request", branch="feature") for name in REQUIRED_WORKFLOWS]
    assert check_release_gate(REPOSITORY, SHA, fetch=lambda *_: runs)["passed"] is False


def test_stale_commit_runs_do_not_satisfy_the_gate():
    """A green run for another commit cannot authorize this tag."""
    runs = [run(name, sha="b" * 40) for name in REQUIRED_WORKFLOWS]
    assert check_release_gate(REPOSITORY, SHA, fetch=lambda *_: runs)["passed"] is False


def test_any_successful_candidate_satisfies_the_workflow():
    """A retried run may leave an earlier failed attempt behind."""
    runs = all_successful() + [run(REQUIRED_WORKFLOWS[0], conclusion="failure", identifier=99)]
    assert check_release_gate(REPOSITORY, SHA, fetch=lambda *_: runs)["passed"] is True


def test_release_gate_rejects_a_short_sha():
    """An abbreviated or foreign revision never reaches the API."""
    with pytest.raises(ValueError, match="40-character"):
        check_release_gate(REPOSITORY, "abc123", fetch=lambda *_: [])
