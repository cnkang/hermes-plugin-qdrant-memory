"""Verify the Linux workflow's cached installer boundary and process cleanup."""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name == "nt", reason="The Ollama setup workflow uses POSIX bash")


def test_cached_executable_cannot_bypass_the_archive_checksum(tmp_path):
    """A corrupt cached download must fail before any cached executable runs."""
    checksum = hashlib.sha256(b"trusted synthetic archive").hexdigest()
    python = tmp_path / ".quality/venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(f"#!/bin/sh\nprintf '0.40.7 {checksum}\\n'\n")
    python.chmod(0o700)
    temporary = tmp_path / "temporary"
    root = temporary / "quality-ollama"
    binary = root / "bin/ollama"
    binary.parent.mkdir(parents=True)
    marker = temporary / "untrusted-executed"
    binary.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    binary.chmod(0o700)
    (root / "ollama.tar.zst").write_bytes(b"corrupt cached archive")
    tools = tmp_path / "tools"
    tools.mkdir()
    # The Linux workflow requires GNU checksum options; use coreutils on macOS too.
    checksum_tool = shutil.which("gsha256sum") or shutil.which("sha256sum")
    assert checksum_tool is not None, "Install GNU coreutils to run the archive integrity contract"
    (tools / "sha256sum").symlink_to(checksum_tool)
    result = subprocess.run(
        [shutil.which("bash"), str(ROOT / "scripts/start_quality_ollama.sh")],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": str(tools) + os.pathsep + os.environ["PATH"],
            "RUNNER_TEMP": str(temporary),
            "MODEL_PROFILE": "hosted-ollama",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert not marker.exists()
    assert "checksum" in result.stderr.lower()
    assert not (root / "ollama.tar.zst").exists()


@pytest.mark.parametrize("alive", [False, True])
def test_workflow_cleanup_accepts_an_exited_test_owned_process(tmp_path, alive):
    """Execute the actual cleanup step against a disposable child, alive or already exited."""
    workflow = YAML(typ="safe").load(
        (ROOT / ".github/workflows/owner-model-quality.yml").read_text()
    )
    step = next(
        row
        for row in workflow["jobs"]["quality"]["steps"]
        if row.get("name") == "Stop the test-owned Ollama process"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)" if alive else "pass"]
    )
    try:
        if not alive:
            child.wait(timeout=5)
        pid = tmp_path / "quality-ollama/pid"
        pid.parent.mkdir()
        pid.write_text(str(child.pid))
        result = subprocess.run(
            [shutil.which("bash"), "-e", "-c", step["run"]],
            env={**os.environ, "RUNNER_TEMP": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
