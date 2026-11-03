"""The Codex cloud lane inherits the existing main-only protected environment."""

import re
from pathlib import Path

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
