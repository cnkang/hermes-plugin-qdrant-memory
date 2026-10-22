"""Execute the workflow gate against success, failure, skipped and cancelled inputs."""

import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from ruamel.yaml import YAML

CASES = (
    [("success",) * 8 + ("true", 0)]
    + [
        tuple(result if position == failed else "success" for position in range(8)) + ("true", 1)
        for failed in range(8)
        for result in ("failure", "skipped", "cancelled")
    ]
    + [
        # A pull request and other branch pushes never run the credentialed
        # dependency scan, so the gate accepts its skip there.
        ("success",) * 5 + ("success", "skipped", "success", "false", 0),
        # The main tree must produce a real dependency scan result.
        ("success",) * 5 + ("success", "skipped", "success", "true", 1),
        ("success",) * 8 + ("false", 0),
    ]
)


def test_required_gate_cannot_skip_on_workflow_cancellation():
    """A skipped required job permits merge, so cancellation must still run the gate."""
    path = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
    gate = YAML(typ="safe").load(path.read_text(encoding="utf-8"))["jobs"]["scan-gate"]
    assert gate["if"] == "always()", "The required gate must run even after cancellation"


def test_snyk_dependency_scan_stays_on_main():
    """The organization token must never run against contributor-controlled code."""
    path = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
    jobs = YAML(typ="safe").load(path.read_text(encoding="utf-8"))["jobs"]
    assert jobs["snyk"]["if"] == "github.ref == 'refs/heads/main'"
    dependency_lock = json.dumps(jobs["dependency-lock"])
    assert "secrets." not in dependency_lock
    assert "check_dependency_inputs.py" in dependency_lock
    assert "persist-credentials" in dependency_lock


@pytest.mark.parametrize(
    "lint,tests,mcp,host_pins,dep_lock,sonar,snyk,snyk_code,main_ref,expected", CASES
)
@pytest.mark.skipif(os.name == "nt", reason="The checked workflow step is POSIX Bash.")
def test_required_gate_executes_actual_workflow_step(
    lint, tests, mcp, host_pins, dep_lock, sonar, snyk, snyk_code, main_ref, expected
):
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
            "MCP_RESULT": mcp,
            "HOST_PINS_RESULT": host_pins,
            "DEP_LOCK_RESULT": dep_lock,
            "SONAR_RESULT": sonar,
            "SNYK_RESULT": snyk,
            "SNYK_CODE_RESULT": snyk_code,
            "MAIN_REF": main_ref,
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


@pytest.mark.parametrize("authorized,available", [("false", False), ("true", True)])
@pytest.mark.skipif(os.name == "nt", reason="The checked workflow step is POSIX Bash.")
def test_existing_cloud_test_key_requires_explicit_opt_in(tmp_path, authorized, available):
    """Execute availability logic: a broad test key must not enable the lane by default."""
    path = Path(__file__).resolve().parents[1] / ".github/workflows/cloud-smoke.yml"
    workflow = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    assert (
        workflow["on"]["workflow_dispatch"]["inputs"]["use_existing_test_key"]["default"] is False
    )
    step = workflow["jobs"]["owner-availability"]["steps"][0]
    assert "github.event_name == 'workflow_dispatch'" in step["env"]["USE_EXISTING_TEST_KEY"]
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        env={
            **os.environ,
            "QDRANT_CLOUD_URL": "https://fixture.cloud.qdrant.io",
            "QDRANT_CLOUD_OWNER_API_KEY": "",
            "QDRANT_CLOUD_OWNER_COLLECTION": "",
            "QDRANT_CLOUD_API_KEY": "synthetic-test-key",
            "USE_EXISTING_TEST_KEY": authorized,
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert output.read_text().strip() == f"available={str(available).lower()}"
    assert "synthetic-test-key" not in result.stdout + result.stderr


def test_independent_cloud_cleanup_uses_supported_client_lifecycle(tmp_path, monkeypatch):
    """Execute the workflow cleanup against a real client that has no context manager."""
    import qdrant_client
    from qdrant_client.models import Distance, VectorParams

    path = Path(__file__).resolve().parents[1] / ".github/workflows/cloud-smoke.yml"
    workflow = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    step = next(
        s
        for s in workflow["jobs"]["owner"]["steps"]
        if s.get("name") == "Verify disposable collection cleanup independently"
    )
    collection = "qm_owner_ci_" + uuid.uuid4().hex
    client = qdrant_client.QdrantClient(":memory:")
    client.create_collection(
        collection, vectors_config=VectorParams(size=3, distance=Distance.COSINE)
    )
    client.create_collection(
        "unrelated", vectors_config=VectorParams(size=3, distance=Distance.COSINE)
    )
    close = client.close
    closed = []
    monkeypatch.setattr(client, "close", lambda: closed.append(True))
    monkeypatch.setattr(qdrant_client, "QdrantClient", lambda **kwargs: client)
    (tmp_path / "cloud-owner-config.json").write_text(
        json.dumps(
            {
                "url": "https://fixture.cloud.qdrant.io",
                "test_api_key": "synthetic-test-key",
                "owner_collection": collection,
                "provision_started": True,
            }
        )
    )
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    try:
        exec("\n".join(step["run"].splitlines()[1:-1]), {})
        assert not client.collection_exists(collection)
        assert client.collection_exists("unrelated")
        assert closed == [True]
    finally:
        close()
