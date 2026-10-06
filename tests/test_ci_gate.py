"""Execute the workflow gate against success, failure, skipped and cancelled inputs."""

import os
import subprocess
from pathlib import Path

import pytest
from ruamel.yaml import YAML

CASES = [("success", "success", "success", "success", "success", 0)] + [
    tuple(result if position == failed else "success" for position in range(5)) + (1,)
    for failed in range(5)
    for result in ("failure", "skipped", "cancelled")
]


@pytest.mark.parametrize("lint,tests,sonar,snyk,snyk_code,expected", CASES)
def test_required_gate_executes_actual_workflow_step(lint, tests, sonar, snyk, snyk_code, expected):
    """Verify required gate executes actual workflow step."""
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )
    command = YAML(typ="safe").load(workflow)["jobs"]["scan-gate"]["steps"][0]["run"]
    result = subprocess.run(
        ["bash", "-e", "-c", command],
        env={
            **os.environ,
            "LINT_RESULT": lint,
            "TEST_RESULT": tests,
            "SONAR_RESULT": sonar,
            "SNYK_RESULT": snyk,
            "SNYK_CODE_RESULT": snyk_code,
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == expected


def test_cloud_smoke_push_trigger_excludes_codex_branches():
    """Assert cloud-smoke.yml push trigger is restricted to main only."""
    workflow_path = Path(__file__).resolve().parents[1] / ".github/workflows/cloud-smoke.yml"
    workflow = YAML(typ="safe").load(workflow_path.read_text(encoding="utf-8"))
    push_branches = workflow["on"]["push"]["branches"]
    assert push_branches == ["main"], (
        f"cloud-smoke push branches must be ['main'], got {push_branches}"
    )
    for branch in push_branches:
        assert "codex" not in branch, f"codex pattern must not appear in push branches: {branch}"


def test_cloud_smoke_uses_qdrant_cloud_environment():
    """Bind Cloud credentials to the dedicated GitHub Environment."""
    workflow_path = Path(__file__).resolve().parents[1] / ".github/workflows/cloud-smoke.yml"
    workflow = YAML(typ="safe").load(workflow_path.read_text(encoding="utf-8"))
    assert workflow["jobs"]["cloud"]["environment"] == "qdrant-cloud"
