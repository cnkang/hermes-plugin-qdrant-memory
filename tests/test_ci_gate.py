import os
from pathlib import Path
import subprocess
import textwrap
import pytest


CASES = [("success", "success", "success", 0)] + [
    tuple(result if position == failed else "success" for position in range(3)) + (1,)
    for failed in range(3) for result in ("failure", "skipped", "cancelled")
]


@pytest.mark.parametrize("tests,sonar,snyk,expected", CASES)
def test_required_gate_executes_actual_workflow_step(tests, sonar, snyk, expected):
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    gate = workflow.split("\n  scan-gate:\n", 1)[1]
    command = textwrap.dedent(gate.split("        run: |\n", 1)[1])
    result = subprocess.run(["bash", "-e", "-c", command], env={
        **os.environ, "TEST_RESULT": tests, "SONAR_RESULT": sonar, "SNYK_RESULT": snyk,
    }, capture_output=True, text=True, timeout=5)
    assert result.returncode == expected
