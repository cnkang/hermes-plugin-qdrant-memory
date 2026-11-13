"""Execute the required security gate and assert the workflow trust policy."""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from ci_gate import ALWAYS_REQUIRED, evaluate  # noqa: E402

WORKFLOWS = ROOT / ".github/workflows"


def _workflow(name):
    """Load one workflow document."""
    return YAML(typ="safe").load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _needs(**overrides):
    """Build a complete needs payload with the policy default for each job."""
    payload = {job: {"result": "success", "outputs": {}} for job in ALWAYS_REQUIRED}
    payload.update(
        {
            "sonarcloud": {"result": "success", "outputs": {}},
            "snyk": {"result": "skipped", "outputs": {}},
            "snyk-code": {"result": "skipped", "outputs": {}},
            "dependency-review": {"result": "success", "outputs": {}},
        }
    )
    payload.update(overrides)
    return payload


def _gate(needs, *, event="pull_request", fork=False, dependabot=False, main=False):
    """Evaluate one scenario."""
    return evaluate(
        needs,
        event_name=event,
        fork_pr=fork,
        dependabot=dependabot,
        main_ref=main,
    )


def test_fork_pull_request_passes_with_credentialed_skips():
    """A fork pull request must pass while its credentialed scans stay skipped."""
    needs = _needs(
        sonarcloud={"result": "skipped"},
        snyk={"result": "skipped"},
        **{"snyk-code": {"result": "skipped"}},
    )
    assert _gate(needs, fork=True) == []


def test_fork_pull_request_cannot_claim_a_credentialed_scan():
    """A successful credentialed scan on a fork means the token was exposed."""
    needs = _needs(snyk={"result": "skipped"}, **{"snyk-code": {"result": "skipped"}})
    problems = _gate(needs, fork=True)
    assert any("sonarcloud=success" in problem for problem in problems)


def test_internal_pull_request_requires_the_quality_gate():
    """An internal pull request must produce a real SonarCloud result."""
    needs = _needs(
        sonarcloud={"result": "skipped"},
        snyk={"result": "skipped"},
        **{"snyk-code": {"result": "skipped"}},
    )
    problems = _gate(needs)
    assert any("sonarcloud=skipped" in problem for problem in problems)


def test_main_push_requires_every_scan():
    """The main tree must produce credentials-backed results."""
    needs = _needs(
        sonarcloud={"result": "success"},
        snyk={"result": "success"},
        **{"snyk-code": {"result": "success"}},
        **{"dependency-review": {"result": "skipped"}},
    )
    assert _gate(needs, event="push", main=True) == []


def test_main_push_fails_when_snyk_is_skipped():
    """A skipped dependency scan on main must fail the gate."""
    needs = _needs(
        snyk={"result": "skipped"},
        **{"snyk-code": {"result": "success"}},
        **{"dependency-review": {"result": "skipped"}},
    )
    problems = _gate(needs, event="push", main=True)
    assert any("snyk=skipped" in problem for problem in problems)


@pytest.mark.parametrize("job", ALWAYS_REQUIRED)
@pytest.mark.parametrize("result", ["failure", "skipped", "cancelled"])
def test_always_required_jobs_never_pass_without_success(job, result):
    """No required job may pass unless GitHub reports success."""
    needs = _needs(**{job: {"result": result}})
    problems = _gate(needs)
    assert any(problem.startswith(f"{job}={result}") for problem in problems)


def test_missing_job_is_a_failure():
    """A removed job must not shrink the required set."""
    needs = _needs()
    del needs["tests"]
    problems = _gate(needs)
    assert any("tests=missing" in problem for problem in problems)


def test_dependency_review_is_required_for_pull_requests_only():
    """Every pull request must evaluate dependency changes."""
    needs = _needs(
        sonarcloud={"result": "success"},
        snyk={"result": "skipped"},
        **{"snyk-code": {"result": "skipped"}, "dependency-review": {"result": "skipped"}},
    )
    problems = _gate(needs)
    assert any("dependency-review=skipped" in problem for problem in problems)


def test_dependabot_pull_request_skips_credentialed_scans():
    """Dependabot pull requests keep the token-free path."""
    needs = _needs(
        sonarcloud={"result": "skipped"},
        snyk={"result": "skipped"},
        **{"snyk-code": {"result": "skipped"}},
    )
    assert _gate(needs, dependabot=True) == []


def test_gate_step_executes_the_committed_script(tmp_path):
    """The workflow must delegate to the reviewed script, not inline logic."""
    gate = _workflow("ci.yml")["jobs"]["scan-gate"]
    assert gate["if"] == "always()", "The required gate must run even after cancellation"
    run = gate["steps"][-1]
    assert run["run"] == "python3 scripts/ci_gate.py"
    assert "NEEDS" in run["env"]

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/ci_gate.py")],
        env={
            **os.environ,
            "NEEDS": json.dumps(_needs(sonarcloud={"result": "success"})),
            "EVENT_NAME": "pull_request",
            "FORK_PR": "false",
            "DEPENDABOT": "false",
            "MAIN_REF": "false",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/ci_gate.py")],
        env={
            **os.environ,
            "NEEDS": json.dumps(_needs(sonarcloud={"result": "skipped"})),
            "EVENT_NAME": "pull_request",
            "FORK_PR": "false",
            "DEPENDABOT": "false",
            "MAIN_REF": "false",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "::error::sonarcloud=skipped" in result.stdout


def test_gate_needs_cover_every_reported_result():
    """Every job the gate evaluates must be declared in needs."""
    gate = _workflow("ci.yml")["jobs"]["scan-gate"]
    assert set(gate["needs"]) == set(_needs())


def test_snyk_jobs_stay_on_main():
    """The organization token must never run against contributor-controlled code."""
    jobs = _workflow("ci.yml")["jobs"]
    assert jobs["snyk"]["if"] == "github.ref == 'refs/heads/main'"
    assert jobs["snyk-code"]["if"] == "github.ref == 'refs/heads/main'"


def test_sonarcloud_skips_forks_and_dependabot_only():
    """SonarCloud runs on trusted trees and skips fork and bot pull requests."""
    condition = _workflow("ci.yml")["jobs"]["sonarcloud"]["if"]
    assert "github.event.pull_request.head.repo.full_name == github.repository" in condition
    assert "github.event.pull_request.user.login == 'dependabot[bot]'" in condition
    assert "github.actor" not in condition


TIER_ONE = (
    "lint",
    "tests",
    "mcp-coverage",
    "codex-windows",
    "host-pins",
    "dependency-lock",
    "dependency-review",
    "workflow-security",
    "secret-scan",
)


@pytest.mark.parametrize("job", TIER_ONE)
def test_credential_free_jobs_hold_no_secret(job):
    """Checks that run on untrusted pull requests must not read secrets."""
    document = json.dumps(_workflow("ci.yml")["jobs"][job])
    assert "secrets." not in document
    assert "SNYK_TOKEN" not in document
    assert "SONAR_TOKEN" not in document


@pytest.mark.parametrize("job", ["workflow-security", "secret-scan", "dependency-review"])
def test_credential_free_jobs_have_a_timeout(job):
    """Every new job must bound its runtime."""
    assert _workflow("ci.yml")["jobs"][job]["timeout-minutes"] <= 15


def test_security_tools_are_pinned():
    """Security tooling must never float."""
    jobs = _workflow("ci.yml")["jobs"]
    zizmor_step = next(
        step
        for step in jobs["workflow-security"]["steps"]
        if step.get("uses", "").startswith("zizmorcore/zizmor-action@")
    )
    assert len(zizmor_step["uses"].split("@")[1]) == 40
    assert zizmor_step["with"]["version"] == "1.32.0"
    assert zizmor_step["with"]["online-audits"] is False

    dependency_step = next(
        step
        for step in jobs["dependency-review"]["steps"]
        if step.get("uses", "").startswith("actions/dependency-review-action@")
    )
    assert len(dependency_step["uses"].split("@")[1]) == 40

    secret = json.dumps(jobs["secret-scan"])
    assert "gitleaks_8.30.1_linux_x64.tar.gz" in secret
    assert "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb" in secret
    assert "--redact" in secret


def test_scheduled_security_has_no_pull_request_trigger():
    """A pull request must never reach the Snyk organization token."""
    workflow = _workflow("scheduled-security.yml")
    triggers = workflow["on"]
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert workflow["jobs"]["snyk-code"]["if"] == "github.ref == 'refs/heads/main'"


def test_ci_lint_python_probe_keeps_the_required_behavior_matrix():
    """Manual lint version selection must not replace required Python coverage."""
    workflow = _workflow("ci.yml")
    version_input = workflow["on"]["workflow_dispatch"]["inputs"]["lint_python_version"]
    assert version_input["required"] is False
    assert version_input["default"] == "3.11"
    assert version_input["type"] == "choice"
    assert version_input["options"] == ["3.11", "3.14"]
    assert workflow["env"]["CI_LINT_PYTHON_VERSION"] == (
        "${{ github.event.inputs.lint_python_version || '3.11' }}"
    )

    lint_steps = workflow["jobs"]["lint"]["steps"]
    validation = next(
        step for step in lint_steps if step.get("name") == "Validate selected lint Python version"
    )
    assert validation["shell"] == "bash"
    assert "3.11|3.14" in validation["run"]
    setup = next(
        step for step in lint_steps if step.get("uses", "").startswith("actions/setup-python@")
    )
    assert setup["with"]["python-version"] == "${{ env.CI_LINT_PYTHON_VERSION }}"
    verify = next(
        step
        for step in lint_steps
        if step.get("name") == "Verify Ruff environment interpreter and install target"
    )
    assert '--expected "$CI_LINT_PYTHON_VERSION"' in verify["run"]
    assert workflow["jobs"]["tests"]["strategy"]["matrix"]["python"] == ["3.11", "3.14"]


def test_snyk_code_pipelines_propagate_failures_through_tee():
    """A successful tee must not hide a failed or timed-out Snyk command."""
    for name in ("ci.yml", "scheduled-security.yml"):
        workflow = _workflow(name)
        scan = next(
            step
            for step in workflow["jobs"]["snyk-code"]["steps"]
            if step.get("name") == "Scan source"
        )
        assert "set -o pipefail" in scan["run"].splitlines()


def test_gitleaks_allowlist_is_limited_to_audited_test_fixtures():
    """Only the two known synthetic fixtures may bypass the secret scanner."""
    allowlist = set((ROOT / ".gitleaksignore").read_text(encoding="utf-8").splitlines())
    assert allowlist == {
        "67766b6996121b7ecfe3bb77e98649bb9303ed4f:tests/test_codex_memory.py:aws-access-token:63",
        "ba4d429b2e8e4d4092ee99c9625ccd206603c113:tests/standalone/test_shared_reader.py:generic-api-key:180",
    }


def test_scorecard_runs_only_on_trusted_events():
    """Scorecard publishes with OIDC and never gates a pull request."""
    workflow = _workflow("scorecard.yml")
    assert set(workflow["on"]) == {"push", "schedule", "workflow_dispatch"}
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["jobs"]["analysis"]["if"] == "github.ref == 'refs/heads/main'"
    permissions = workflow["jobs"]["analysis"]["permissions"]
    assert permissions["id-token"] == "write"
    assert "security-events" not in permissions
    step = next(
        item
        for item in workflow["jobs"]["analysis"]["steps"]
        if item.get("uses", "").startswith("ossf/scorecard-action@")
    )
    assert len(step["uses"].split("@")[1]) == 40
    assert step["with"]["publish_results"] is True


def test_release_gate_requires_code_scanning(tmp_path):
    """A release needs CodeQL evidence for the tagged commit, not only tests."""
    from check_release_gate import REQUIRED_WORKFLOWS, check_release_gate

    assert "CodeQL" in REQUIRED_WORKFLOWS
    sha = "a" * 40

    def fetch(repository, requested):
        """Return one passing run per required workflow."""
        assert requested == sha
        return [
            {
                "name": name,
                "head_sha": sha,
                "event": "push",
                "head_branch": "main",
                "status": "completed",
                "conclusion": "success",
                "id": index,
                "html_url": f"https://example.invalid/{index}",
            }
            for index, name in enumerate(REQUIRED_WORKFLOWS, start=1)
        ]

    report = check_release_gate("cnkang/hermes-plugin-qdrant-memory", sha, fetch=fetch)
    assert report["passed"] is True


def test_verify_manifest_stays_scoped_and_resolvable():
    """The local verify manifest must name real, host-free tests."""
    manifest = json.loads((ROOT / ".hermes/environment.json").read_text(encoding="utf-8"))
    recipe = manifest["recipe"]
    assert manifest["version"] == 1
    assert "smoke" in recipe["name"].lower()
    assert ".test-env/bin/python" in recipe["bootstrap"][0]
    command = recipe["test"][0]
    targets = [token for token in command.split() if token.startswith("tests/")]
    assert targets, "The recipe must name explicit test files"
    for target in targets:
        assert (ROOT / target).is_file(), target
    assert "test_manifest_contract.py" not in command, "That module needs the Hermes host"
    assert recipe["start"] is None


@pytest.mark.parametrize(
    "filename,job", [("ci.yml", "tests"), ("upstream-compat.yml", "latest-hermes")]
)
def test_exported_host_environments_keep_lock_overrides(filename, job):
    """Keep host security overrides when resolving exported host requirements."""
    jobs = _workflow(filename)["jobs"]
    steps = jobs[job]["steps"]
    consumers = []
    for step in steps:
        # Join shell continuations so each install is checked independently.
        commands = step.get("run", "").replace("\\\n", " ").splitlines()
        consumers.extend(
            line for line in commands if "pm.build_env" in line and "--requirements" in line
        )
    assert consumers, "The host compatibility lane must install its exported requirements"
    for command in consumers:
        assert '--lock "$GITHUB_WORKSPACE/.host/uv.lock"' in command


def test_cloud_smoke_push_trigger_excludes_codex_branches():
    """Assert cloud-smoke.yml push trigger is restricted to main only."""
    push_branches = _workflow("cloud-smoke.yml")["on"]["push"]["branches"]
    assert push_branches == ["main"], (
        f"cloud-smoke push branches must be ['main'], got {push_branches}"
    )
    for branch in push_branches:
        assert "codex" not in branch, f"codex pattern must not appear in push branches: {branch}"


def test_cloud_smoke_uses_qdrant_cloud_environment():
    """Bind Cloud credentials to the dedicated GitHub Environment."""
    workflow = _workflow("cloud-smoke.yml")
    assert workflow["jobs"]["cloud"]["environment"] == "qdrant-cloud"


@pytest.mark.parametrize("authorized,available", [("false", False), ("true", True)])
@pytest.mark.skipif(os.name == "nt", reason="The checked workflow step is POSIX Bash.")
def test_existing_cloud_test_key_requires_explicit_opt_in(tmp_path, authorized, available):
    """Execute availability logic: a broad test key must not enable the lane by default."""
    workflow = _workflow("cloud-smoke.yml")
    assert (
        workflow["on"]["workflow_dispatch"]["inputs"]["use_existing_test_key"]["default"] is False
    )
    step = workflow["jobs"]["owner-availability"]["steps"][0]
    assert "github.event_name == 'workflow_dispatch'" in step["env"]["USE_EXISTING_TEST_KEY"]
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        env={
            **os.environ,
            "QDRANT_CLOUD_URL": "https://fixture.cloud.qdrant.io",
            "QDRANT_CLOUD_OWNER_API_KEY": "",
            "QDRANT_CLOUD_OWNER_COLLECTION": "",
            "QDRANT_CLOUD_API_KEY": "synthetic-test-key",
            "USE_EXISTING_TEST_KEY": authorized,
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert output.read_text().strip() == f"available={str(available).lower()}"
    assert "synthetic-test-key" not in result.stdout + result.stderr


def test_independent_cloud_cleanup_uses_supported_client_lifecycle(tmp_path, monkeypatch):
    """Execute the workflow cleanup against a real client that has no context manager."""
    import qdrant_client
    from qdrant_client.models import Distance, VectorParams

    workflow = _workflow("cloud-smoke.yml")
    step = next(
        s
        for s in workflow["jobs"]["owner"]["steps"]
        if s.get("name") == "Verify disposable collection cleanup independently"
    )
    collection = "qm_owner_ci_" + uuid.uuid4().hex
    client = qdrant_client.QdrantClient(":memory:")
    client.create_collection(
        collection, vectors_config=VectorParams(size=3, distance=Distance.COSINE)
    )
    client.create_collection(
        "unrelated", vectors_config=VectorParams(size=3, distance=Distance.COSINE)
    )
    close = client.close
    closed = []
    monkeypatch.setattr(client, "close", lambda: closed.append(True))
    monkeypatch.setattr(qdrant_client, "QdrantClient", lambda **kwargs: client)
    (tmp_path / "cloud-owner-config.json").write_text(
        json.dumps(
            {
                "url": "https://fixture.cloud.qdrant.io",
                "test_api_key": "synthetic-test-key",
                "owner_collection": collection,
                "provision_started": True,
            }
        )
    )
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    try:
        exec("\n".join(step["run"].splitlines()[1:-1]), {})
        assert not client.collection_exists(collection)
        assert client.collection_exists("unrelated")
        assert closed == [True]
    finally:
        close()
