"""Execute the workflow gate against success, failure, skipped and cancelled inputs."""

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

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
    gate = workflow.split("\n  scan-gate:\n", 1)[1]
    command = textwrap.dedent(gate.split("        run: |\n", 1)[1])
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
