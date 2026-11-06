"""Guard the Python matrix against rebuilding environments with another interpreter."""

from pathlib import Path

from ruamel.yaml import YAML

from scripts.verify_ci_python import _expected_pair, _is_within, _version_pair

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
_PYTHON_TARGET = "$(python -c 'import sys; print(sys.executable)')"


def test_python_matrix_version_comparison_uses_major_minor_values():
    """The runtime's decorated sys.version string must not affect matrix checks."""
    assert _expected_pair("3.11") == (3, 11)
    assert _version_pair({"version_info": [3, 11], "version": "3.11.9 (main)"}) == (
        3,
        11,
    )


def test_distribution_install_location_must_belong_to_requested_environment(tmp_path):
    """A preinstalled system package must not satisfy a matrix environment check."""
    environment = tmp_path / "venv"
    metadata = environment / "lib" / "python3.11" / "site-packages" / "pkg.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("Name: pkg\n", encoding="utf-8")
    outside = tmp_path / "system" / "pkg.dist-info" / "METADATA"
    outside.parent.mkdir(parents=True)
    outside.write_text("Name: pkg\n", encoding="utf-8")

    assert _is_within(environment, metadata)
    assert not _is_within(environment, outside)


def test_matrix_builds_pin_the_setup_python_and_report_the_runtime_environment():
    """Every matrix environment build must use and report the selected interpreter."""
    parser = YAML(typ="safe")
    for workflow_name in (
        "ci.yml",
        "platform-compat.yml",
        "upstream-compat.yml",
        "standalone.yml",
    ):
        document = parser.load((WORKFLOWS / workflow_name).read_text(encoding="utf-8"))
        for job in document["jobs"].values():
            steps = job.get("steps", [])
            matrix_python = job.get("strategy", {}).get("matrix", {}).get("python")
            if matrix_python is not None:
                provenance = [
                    step.get("run", "")
                    for step in steps
                    if "verify_ci_python.py" in step.get("run", "")
                ]
                assert any('"${{ matrix.python }}"' in run for run in provenance), (
                    workflow_name,
                    job.get("name", "unnamed job"),
                )
            builds = [
                step.get("run", "")
                for step in steps
                if "python -m pm.build_env" in step.get("run", "")
                and "--out" in step.get("run", "")
            ]
            if not builds:
                continue
            assert all(_PYTHON_TARGET in run for run in builds), workflow_name
            assert any("verify_ci_python.py" in step.get("run", "") for step in steps), (
                workflow_name
            )


def test_behavior_workflows_require_the_plugin_distribution_in_the_test_environment():
    """The three host behavior workflows must verify the installed plugin package."""
    parser = YAML(typ="safe")
    for workflow_name in ("ci.yml", "platform-compat.yml", "upstream-compat.yml"):
        document = parser.load((WORKFLOWS / workflow_name).read_text(encoding="utf-8"))
        verifier_calls = [
            step.get("run", "")
            for job in document["jobs"].values()
            for step in job.get("steps", [])
            if "verify_ci_python.py" in step.get("run", "")
            and '--expected "${{ matrix.python }}"' in step.get("run", "")
            and '--environment "$GITHUB_WORKSPACE/.test-env"' in step.get("run", "")
            and "--distribution hermes-plugin-qdrant-memory" in step.get("run", "")
        ]
        assert len(verifier_calls) == 1, (workflow_name, verifier_calls)
        assert "--require-distribution" in verifier_calls[0], (
            workflow_name,
            verifier_calls[0],
        )
