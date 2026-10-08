"""Exercise closed output pipes in real processes, including interpreter shutdown."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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
@pytest.mark.skipif(os.name == "nt", reason="Closed-pipe shutdown codes differ on Windows.")
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


@pytest.mark.parametrize("mode,count", [("success", 1), ("success", 3000), ("error", 1)])
@pytest.mark.skipif(os.name == "nt", reason="This pipe contract relies on POSIX EPIPE behavior.")
def test_closed_pipe_in_current_process_can_flush_after_failure(monkeypatch, mode, count):
    """Exercise real pipe failures under coverage and ensure later flushes remain safe."""
    from qdrant_memory import cli

    def run(args):
        if mode == "error":
            raise RuntimeError("private-error-text")
        return {"target_ids": ["x" * 100] * count}

    monkeypatch.setattr(cli, "run", run)
    reader, writer = os.pipe()
    os.close(reader)
    with os.fdopen(writer, "w") as output:
        with monkeypatch.context() as capture:
            capture.setattr(sys, "stdout", output)
            assert cli.main(SimpleNamespace(qdrant_command="status")) == 1
            output.flush()
            # Also exercise the interpreter's later write/flush behavior.
            output.write("shutdown flush")
            output.flush()
