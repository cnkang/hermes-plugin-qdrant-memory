"""The Codex cloud lane inherits the existing main-only protected environment."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]


def test_cloud_lane_is_manual_main_only_and_uses_existing_approval():
    """Do not expose environment credentials to PRs or caller-selected code."""
    path = ROOT / ".github/workflows/codex-plugin-cloud.yml"
    text = path.read_text(encoding="utf-8")
    workflow = YAML(typ="safe").load(text)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    preflight = workflow["jobs"]["preflight"]["steps"][0]["run"]
    assert 'test "$REF" = refs/heads/main' in preflight
    assert 'test "$REPOSITORY" = cnkang/hermes-plugin-qdrant-memory' in preflight
    admitted = workflow["jobs"]["preflight"]
    assert admitted["steps"][0]["id"] == "checked"
    assert admitted["steps"][0]["env"]["RUNTIME_SHA"] == "${{ github.sha }}"
    assert admitted["outputs"]["runtime_sha"] == "${{ steps.checked.outputs.runtime_sha }}"
    assert '"$RUNTIME_SHA" >> "$GITHUB_OUTPUT"' in preflight
    cloud = workflow["jobs"]["cloud"]
    assert cloud["needs"] == "preflight"
    assert cloud["environment"] == "soak-cloud"
    assert "deployment: false" not in text
    checkout = cloud["steps"][0]["with"]
    assert checkout["repository"] == "cnkang/qdrant-memory-mcp"
    assert re.fullmatch("[0-9a-f]{40}", checkout["ref"])
    assert checkout["persist-credentials"] is False
    install = next(
        step
        for step in cloud["steps"]
        if step.get("name") == "Install pinned Codex and private Runtime environment"
    )
    assert install["env"]["VERIFIED_RUNTIME_SHA"] == "${{ needs.preflight.outputs.runtime_sha }}"
    fixed = "-m pip install -r client/plugins/qdrant-memory/requirements.txt"
    override = "-m pip install --no-deps --force-reinstall"
    assert install["run"].index(fixed) < install["run"].index(override)
    assert "hermes-plugin-qdrant-memory.git@${VERIFIED_RUNTIME_SHA}" in install["run"]
    assert 'source["vcs_info"]["commit_id"] == os.environ["VERIFIED_RUNTIME_SHA"]' in install["run"]
    credential_steps = [s for s in cloud["steps"] if "SOAK_LLM_API_KEY" in s.get("env", {})]
    assert len(credential_steps) == 1
    step = credential_steps[0]
    assert step["env"]["SOAK_LLM_API_KEY"] == "${{ secrets.SOAK_LLM_API_KEY }}"
    assert "--mode cloud" in step["run"]
    assert "SOAK_QDRANT_API_KEY" not in text
    assert "github.event.inputs" not in text


@pytest.mark.parametrize(
    "raw,expected,preserve",
    [
        (None, "NOT VERIFIED", False),
        (b"", "NOT VERIFIED", False),
        (b"{", "NOT VERIFIED", False),
        (b"\xff", "NOT VERIFIED", False),
        (b"[]", "NOT VERIFIED", False),
        (b'{"status":"UNKNOWN"}', "NOT VERIFIED", False),
        (b'{"count":3}', "NOT VERIFIED", False),
        (b'{ "status": "NOT VERIFIED", "reason": "fixture" }\n', "NOT VERIFIED", True),
        (b'{"status":"PASS","count":3}', "PASS", True),
        (b'{"status":"FAIL","detail":"fixture"}', "FAIL", True),
    ],
)
def test_cloud_summary_retains_an_explicit_result_artifact(tmp_path, raw, expected, preserve):
    """Preserve valid results and publish a fallback after interrupted setup."""
    workflow = YAML(typ="safe").load(
        (ROOT / ".github/workflows/codex-plugin-cloud.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["cloud"]["steps"]
    summary = next(s for s in steps if s.get("name") == "Summarize explicit outcome")
    assert summary["if"] == "always()"
    assert summary["shell"] == "python"
    output = tmp_path / "codex-plugin-result.json"
    if raw is not None:
        output.write_bytes(raw)
    report = tmp_path / "summary.md"
    report.write_text("Previous summary\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", summary["run"]],
        env={**os.environ, "RUNNER_TEMP": str(tmp_path), "GITHUB_STEP_SUMMARY": str(report)},
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert artifact["status"] == expected
    if preserve:
        assert output.read_bytes() == raw
    rendered = report.read_text(encoding="utf-8")
    assert rendered.startswith("Previous summary\n")
    assert json.dumps(artifact, indent=2) in rendered
    assert steps[-1]["with"]["if-no-files-found"] == "error"
