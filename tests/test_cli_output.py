"""Exercise closed output pipes in real processes, including interpreter shutdown."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = """
import sys
from types import SimpleNamespace
from qdrant_memory import cli

def run(args):
    if sys.argv[1] == 'error':
        raise RuntimeError('private-error-text')
    return {'target_ids': ['x' * 100] * int(sys.argv[2])}

cli.run = run
sys.stdin.buffer.read(1)
sys.exit(cli.main(SimpleNamespace(qdrant_command='status')))
"""


@pytest.mark.parametrize("mode,count", [("success", 1), ("success", 3000), ("error", 1)])
def test_closed_stdout_exits_without_traceback(mode, count):
    """Closing the reader before printing covers both buffered and large outputs."""
    process = subprocess.Popen(
        [sys.executable, "-c", SCRIPT, mode, str(count)],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    process.stdout.close()
    process.stdout = None
    _, stderr = process.communicate(b"x", timeout=15)
    assert process.returncode == 1
    assert stderr == b""


@pytest.mark.parametrize("mode,expected_code", [("success", 0), ("error", 1)])
def test_open_stdout_preserves_json_and_exit_status(mode, expected_code):
    """Normal output remains valid JSON and operational errors remain failures."""
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT, mode, "1"],
        cwd=ROOT,
        input=b"x",
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == expected_code
    assert result.stderr == b""
    output = json.loads(result.stdout)
    if mode == "error":
        assert output == {"error": {"type": "RuntimeError", "retryable": False}}
        assert b"private-error-text" not in result.stdout
    else:
        assert output == {"target_ids": ["x" * 100]}
