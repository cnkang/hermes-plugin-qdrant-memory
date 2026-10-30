"""Static gates for the soak workflow: triggers, environments and control flow."""

import os
import re
import subprocess
from pathlib import Path

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "soak.yml"
SHA = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")


def workflow():
    """Load the soak workflow as a plain mapping."""
    return YAML(typ="safe").load(WORKFLOW.read_text(encoding="utf-8"))


def needs_of(job):
    """Return a job's needs as a list."""
    value = job.get("needs", [])
    return [value] if isinstance(value, str) else list(value)


def steps(job):
    """Return a job's steps as a list of mappings."""
    return job.get("steps") or []


def runs(job):
    """Return every run script of a job joined into one string."""
    return "\n".join(step.get("run", "") for step in steps(job))


def test_only_manual_and_scheduled_triggers():
    """No push or pull_request lane, so an untrusted PR can never reach a cloud key."""
    triggers = workflow()["on"]
    assert set(triggers) == {"workflow_dispatch", "schedule"}
    assert triggers["workflow_dispatch"]["inputs"]["include_cloud"]["default"] is False
    assert triggers["workflow_dispatch"]["inputs"]["mode"]["default"] == "quick"
    assert triggers["schedule"][0]["cron"] == "0 3 * * 0"


def test_workflow_permissions_are_read_only():
    """The workflow never asks for a write scope anywhere."""
    document = workflow()
    assert document["permissions"] == {"contents": "read"}
    for name, job in document["jobs"].items():
        permissions = job.get("permissions") or {}
        assert set(permissions) <= {"contents", "actions"}, name
        assert all(value == "read" for value in permissions.values()), name
        assert "write" not in str(permissions), name


def test_concurrency_never_cancels_a_running_soak():
    """An expensive window must not be cancelled by a later dispatch."""
    concurrency = workflow()["concurrency"]
    assert concurrency["group"] == "soak-${{ github.ref }}"
    assert concurrency["cancel-in-progress"] is False


def test_preflight_validates_the_workflow_and_the_harness():
    """Static validation and the harness contract tests run before any runner is spent."""
    job = workflow()["jobs"]["preflight"]
    script = runs(job)
    assert "actionlint" in script
    assert "sha256sum -c" in script, "the downloaded linter must be checksum verified"
    assert "1.7.12" in script
    assert "pytest tests/soak" in script
    assert "steps.profile.outputs.mode" in job["outputs"]["mode"]
    assert "soak-cloud environment allows only main" in script
    assert "refs/heads/main" in script


def test_smoke_matrix_covers_six_legs_without_fail_fast(tmp_path):
    """Both architectures and all three scenarios run in parallel on hosted runners."""
    job = workflow()["jobs"]["smoke"]
    matrix = job["strategy"]["matrix"]
    assert matrix["runner"] == ["ubuntu-24.04", "ubuntu-24.04-arm"]
    assert matrix["scenario"] == ["steady", "restart", "network"]
    assert job["strategy"]["fail-fast"] is False
    assert job["timeout-minutes"] <= 30
    assert job["environment"] == {"name": "soak-local", "deployment": False}
    script = runs(job)
    assert "--profile quick" in script
    assert "--target-seconds 480" in script
    uploads = [step for step in steps(job) if "upload-artifact" in step.get("uses", "")]
    assert uploads and uploads[0]["if"] == "always()"
    assert uploads[0]["with"]["if-no-files-found"] == "error"


def test_local_smoke_gate_requires_every_leg_and_runs_after_a_failure():
    """A skipped, cancelled or missing leg can never read as a pass."""
    job = workflow()["jobs"]["local-smoke-gate"]
    assert job["if"] == "always()"
    assert set(needs_of(job)) == {"preflight", "smoke"}
    assert "steps.decide.outputs.ready" in job["outputs"]["ready"]
    script = runs(job)
    assert "--stage smoke" in script
    assert "--reports artifacts/smoke" in script
    assert "ready=false" in script and "ready=true" in script
    assert "exit 1" in script


def test_cloud_job_uses_only_the_soak_cloud_environment():
    """Cloud credentials live in exactly one environment and one job."""
    document = workflow()
    job = document["jobs"]["cloud-smoke"]
    assert job["environment"] == "soak-cloud"
    assert "deployment" not in job["environment"], (
        "deployment: false is incompatible with the required-reviewer rule"
    )
    condition = " ".join(job["if"].split())
    assert "needs.preflight.outputs.include_cloud == 'true'" in condition
    assert "needs.local-smoke-gate.outputs.ready == 'true'" in condition
    script = runs(job)
    assert "SOAK_QDRANT_URL" in script and "SOAK_QDRANT_API_KEY" in script
    assert "--backend cloud" in script
    assert "Settings > Environments > soak-cloud" in script
    assert "exit 1" in script, "an incomplete cloud contract must fail, not skip"
    assert "scripts.soak.cloud" in script, "the cleanup audit must run independently"
    for name, other in document["jobs"].items():
        if name == "cloud-smoke":
            continue
        text = str(other)
        assert "secrets.SOAK_" not in text, name
        assert "vars.SOAK_" not in text, name


def test_admission_gate_blocks_the_extended_soak_until_smoke_passes():
    """Full mode needs a green smoke gate; quick mode never schedules the long run."""
    job = workflow()["jobs"]["admission-gate"]
    assert job["if"] == "always()"
    assert set(needs_of(job)) == {"preflight", "local-smoke-gate", "cloud-smoke"}
    script = runs(job)
    assert 'if [ "$MODE" != "full" ]' in script
    assert '"$SMOKE_RESULT" != "success"' in script
    assert '"$CLOUD_RESULT" != "success"' in script
    assert "ready=$ready" in script
    soak = workflow()["jobs"]["soak"]
    assert soak["if"] == "needs.admission-gate.outputs.ready == 'true'"
    assert set(needs_of(soak)) == {"preflight", "admission-gate"}


def test_extended_soak_runs_the_full_window_inside_the_hard_timeout():
    """Five hours of load fits a 350-minute job with room for evidence."""
    job = workflow()["jobs"]["soak"]
    assert 350 <= job["timeout-minutes"] <= 355
    assert job["strategy"]["fail-fast"] is False
    assert job["environment"] == {"name": "soak-local", "deployment": False}
    script = runs(job)
    assert "--profile full" in script
    assert "--target-seconds 18000" in script
    matrix = job["strategy"]["matrix"]
    assert matrix["runner"] == ["ubuntu-24.04", "ubuntu-24.04-arm"]
    assert matrix["scenario"] == ["steady", "restart", "network"]


def test_final_report_always_runs_and_requires_the_artifacts():
    """A failed or cancelled matrix still produces a final verdict, never a pass."""
    job = workflow()["jobs"]["final-report"]
    assert job["if"] == "always()"
    assert set(needs_of(job)) >= {
        "preflight",
        "smoke",
        "local-smoke-gate",
        "cloud-smoke",
        "admission-gate",
        "soak",
    }
    downloads = [step for step in steps(job) if "download-artifact" in step.get("uses", "")]
    assert len(downloads) >= 2
    script = runs(job)
    assert "--stage smoke" in script
    assert "--stage soak" in script
    assert 'if [ "$MODE" = "full" ]' in script
    assert "verdict=1" in script and 'exit "$verdict"' in script
    # The stage results are checked as well, so a cancelled matrix cannot pass.
    assert '"$SOAK_RESULT" != "success"' in script
    assert '"$CLOUD_RESULT" != "success"' in script
    assert '--commit "$COMMIT_SHA"' in script
    uploads = [step for step in steps(job) if "upload-artifact" in step.get("uses", "")]
    assert uploads and uploads[0]["with"]["name"] == "aggregate-final"


def test_every_action_is_pinned_to_a_commit_sha():
    """Third-party actions are immutable; a tag would be mutable input."""
    for name, job in workflow()["jobs"].items():
        for step in steps(job):
            uses = step.get("uses")
            if uses is None:
                continue
            assert SHA.match(uses), f"{name} uses an unpinned action: {uses}"


def test_workflow_and_runner_windows_match_the_harness_constants():
    """The workflow cannot silently shorten or lengthen a documented window."""
    from scripts.soak.common import PROFILE_SECONDS

    script = runs(workflow()["jobs"]["smoke"])
    assert f"--target-seconds {PROFILE_SECONDS['quick']}" in script
    assert f"--target-seconds {PROFILE_SECONDS['full']}" in runs(workflow()["jobs"]["soak"])


def test_a_docker_image_is_never_pulled_without_a_digest():
    """The harness pins both images by digest; the workflow must not add a tag pull."""
    prepare = (ROOT / "scripts" / "soak" / "prepare.py").read_text(encoding="utf-8")
    images = re.findall(r'"(qdrant/qdrant|ghcr\.io/[^"]+)@sha256:[0-9a-f]{64}"', prepare)
    assert "qdrant/qdrant" in images
    assert any("toxiproxy" in image for image in images)
    assert "docker pull" not in runs(workflow()["jobs"]["smoke"])


def _step_script(job, needle):
    """Return the run script of the first step whose name contains ``needle``."""
    for step in steps(job):
        if needle in (step.get("name") or ""):
            return step["run"]
    raise AssertionError(f"no step named {needle!r}")


def test_cloud_contract_check_fails_instead_of_skipping(tmp_path):
    """An enabled cloud window with a missing name must fail the job."""
    script = _step_script(workflow()["jobs"]["cloud-smoke"], "Require the soak-cloud contract")
    command = ["bash", "-euo", "pipefail", "-c", script]

    # GitHub injects empty strings for a name that is not configured.
    empty = subprocess.run(
        command,
        env={
            "PATH": os.environ["PATH"],
            "SOAK_QDRANT_URL": "",
            "SOAK_QDRANT_COLLECTION": "",
            "SOAK_QDRANT_API_KEY": "",
        },
        capture_output=True,
        text=True,
    )
    assert empty.returncode == 1
    assert "SOAK_QDRANT_URL(variable)" in empty.stdout + empty.stderr
    assert "SOAK_QDRANT_API_KEY(secret)" in empty.stdout + empty.stderr

    complete = subprocess.run(
        command,
        env={
            "PATH": os.environ["PATH"],
            "SOAK_QDRANT_URL": "https://example.invalid",
            "SOAK_QDRANT_COLLECTION": "qm_soak_test",
            "SOAK_QDRANT_API_KEY": "placeholder",
        },
        capture_output=True,
        text=True,
    )
    assert complete.returncode == 0
    assert "present" in complete.stdout


def test_the_cloud_report_is_aggregated_from_its_own_artifact_tree():
    """The smoke aggregate must not look for the cloud leg among smoke artifacts."""
    script = runs(workflow()["jobs"]["final-report"])
    smoke_call = script.split("--stage smoke", 1)[1].split("--output", 1)[0]
    assert "--cloud" not in smoke_call, "the smoke aggregate owns only the smoke legs"
    assert "--stage cloud" in script
    assert "--reports artifacts/cloud" in script
    assert "--platform cloud --scenario steady" in script


def test_no_credential_reaches_a_command_line():
    """Every cloud step reads its credentials from the step environment."""
    for job in ("cloud-smoke",):
        script = runs(workflow()["jobs"][job])
        assert '--api-key "$SOAK_QDRANT_API_KEY"' not in script
        assert '--cloud-api-key "$SOAK_QDRANT_API_KEY"' not in script
        assert "--cloud-llm-key" not in script
    prepare = (ROOT / "scripts" / "soak" / "run.py").read_text(encoding="utf-8")
    assert 'None if args.backend == "cloud" else env.ca_file' in prepare


def test_cloud_cleanup_never_deletes_the_fixed_collection():
    """The fixed test collection survives every run; only run records are removed."""
    source = (ROOT / "scripts" / "soak" / "prepare.py").read_text(encoding="utf-8")
    assert "delete_collection" not in source
    assert "delete_run_records" in source
    audit = (ROOT / "scripts" / "soak" / "cloud.py").read_text(encoding="utf-8")
    assert "collection_retained" in audit
    assert "delete_collection" not in audit


@pytest.mark.parametrize("required", ["report.json", "events.jsonl", "metrics.json", "junit.xml"])
def test_each_runner_uploads_the_documented_artifacts(required):
    """The harness writes the four evidence files the aggregate validates."""
    source = (ROOT / "scripts" / "soak" / "run.py").read_text(encoding="utf-8")
    assert required in source
    aggregate = (ROOT / "scripts" / "soak" / "aggregate.py").read_text(encoding="utf-8")
    assert required in aggregate
