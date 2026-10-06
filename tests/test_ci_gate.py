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
