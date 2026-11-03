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
    cloud = workflow["jobs"]["cloud"]
    assert cloud["needs"] == "preflight"
    assert cloud["environment"] == "soak-cloud"
    assert "deployment: false" not in text
    checkout = cloud["steps"][0]["with"]
    assert checkout["repository"] == "cnkang/qdrant-memory-mcp"
    assert re.fullmatch("[0-9a-f]{40}", checkout["ref"])
    assert checkout["persist-credentials"] is False
    credential_steps = [s for s in cloud["steps"] if "SOAK_LLM_API_KEY" in s.get("env", {})]
    assert len(credential_steps) == 1
    step = credential_steps[0]
    assert step["env"]["SOAK_LLM_API_KEY"] == "${{ secrets.SOAK_LLM_API_KEY }}"
    assert "--mode cloud" in step["run"]
    assert "SOAK_QDRANT_API_KEY" not in text
    assert "github.event.inputs" not in text
