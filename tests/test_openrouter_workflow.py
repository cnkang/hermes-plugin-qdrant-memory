"""Optional cloud jobs cannot expose test credentials to ordinary PR smoke."""

from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]


def workflow():
    """Read workflow strings without YAML 1.1 rewriting the on key."""
    return YAML(typ="base").load((ROOT / ".github/workflows/owner-model-quality.yml").read_text())


def test_cloud_profiles_share_one_account_concurrency_group():
    """Two manual cloud tiers must not multiply the free account's request rate."""
    group = workflow()["concurrency"]["group"]
    assert "startsWith(inputs.profile, 'openrouter-')" in group
    assert "'cloud-account'" in group
    assert workflow()["concurrency"]["cancel-in-progress"] == "false"


def test_manual_cloud_tiers_and_both_inputs_are_available():
    """Model retirement can be handled by explicit inputs without source changes."""
    inputs = workflow()["on"]["workflow_dispatch"]["inputs"]
    assert {"openrouter-cloud-smoke", "openrouter-cloud-quality", "openrouter-free"} <= set(
        inputs["profile"]["options"]
    )
    assert inputs["profile"]["default"] == "openrouter-cloud-quality"
    assert inputs["generation_model"]["default"] == inputs["embedding_model"]["default"] == ""


def test_all_four_variables_are_injected_without_becoming_secrets():
    """Only the API key is required; empty optional Variables reach profile defaults."""
    steps = workflow()["jobs"]["quality"]["steps"]
    cloud = [s for s in steps if "OPENROUTER_API_KEY" in s.get("env", {})]
    assert len(cloud) == 1
    env = cloud[0]["env"]
    for tier in ("SMOKE", "QUALITY"):
        for kind in ("CHAT", "EMBEDDING"):
            name = f"OPENROUTER_{tier}_{kind}_MODEL"
            assert f"vars.{name}" in env[name]
    assert "OPENROUTER_QUALITY_MODEL" in env
    assert "inputs.generation_model" in env["OPENROUTER_CHAT_MODEL_INPUT"]
    assert "inputs.embedding_model" in env["OPENROUTER_EMBEDDING_MODEL_INPUT"]
    assert "refs/heads/main" in cloud[0]["if"]


def test_local_smoke_keeps_five_repetitions_and_no_api_key():
    """Remote smoke uses one run while required local smoke retains its stronger gate."""
    job = workflow()["jobs"]["quality"]
    assert "OPENROUTER_API_KEY" not in job.get("env", {})
    local = [s for s in job["steps"] if s.get("name") == "Actual small-model lifecycle smoke"][0]
    assert "--repetitions 5" in local["run"]
    assert "OPENROUTER_API_KEY" not in local.get("env", {})
    cloud = [s for s in job["steps"] if "OPENROUTER_API_KEY" in s.get("env", {})][0]
    assert "--repetitions 1" in cloud["run"]
    assert "SMOKE_PASS" in str(job["steps"])


def test_remote_tiers_do_not_download_ollama_or_local_models():
    """Remote-only profiles retain dependency caches without model downloads."""
    steps = workflow()["jobs"]["quality"]["steps"]
    setup = [s for s in steps if s.get("name") == "Install and start isolated Ollama models"][0]
    assert "local-required" in setup["if"]
    names = {"Cache the fixed Ollama archive", "Cache fixed local model manifests and blobs"}
    caches = [s for s in steps if s.get("name") in names]
    assert len(caches) == 2
    assert all(s["if"] == "steps.model-cache-keys.outputs.local-required == 'true'" for s in caches)
    artifacts = [s for s in steps if "actions/upload-artifact@" in s.get("uses", "")]
    assert artifacts and all(s["if"] == "always()" for s in artifacts)
