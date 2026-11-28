"""Keep Docker test servers and the installed client on reviewed combinations."""

import io
import runpy
import shlex
import sys
import tomllib
from importlib.metadata import version as installed_version
from pathlib import Path
from urllib.error import URLError

import pytest
from ruamel.yaml import YAML

from scripts.soak.prepare import QDRANT_IMAGE

ROOT = Path(__file__).resolve().parents[2]
CLIENT_PIN = "qdrant-client==1.20.0"
CONTROLLED_CLIENT = "1.20.0"
SERVER_VERSION = "1.20.1"
DEVICE_COMPAT_SERVER = {
    "version": "1.20.2",
    "image": "qdrant/qdrant@sha256:d277592721d2e3b73f9b797b53cf05e6a5bb2134f6d13fdbda4ca51782afbcac",
}
PREVIOUS_SERVER_IMAGE = (
    "qdrant/qdrant@sha256:9bfc89f9914fc14876a88c05270685921a55b8877b254a98bcc1cc2f0a424cd2"
)
LANES = (
    (
        "ci",
        "tests",
        ("services", "qdrant", "image"),
        "Build disposable test environment with Hermes PM",
    ),
    ("shared-security", "device-server", ("services", "qdrant", "image"), None),
    (
        "shared-security",
        "protected-owner",
        ("env", "QDRANT_TEST_IMAGE"),
        "Install a compatible Qdrant test pair",
    ),
    ("soak", "preflight", None, "Install the harness test dependencies"),
    ("soak", "smoke", None, "Install the harness dependencies"),
    ("soak", "cloud-smoke", None, "Install the harness dependencies"),
    ("soak", "soak", None, "Install the harness dependencies"),
)


def workflows():
    """Load independent workflow documents for lane checks and drift probes."""
    return {
        name: YAML(typ="safe").load((ROOT / f".github/workflows/{name}.yml").read_text())
        for name in ("ci", "shared-security", "soak")
    }


def install_step(job, name):
    """Select the one installation step owned by this validation lane."""
    if name is None:
        matches = [
            step
            for step in job["steps"]
            if step.get("run", "").startswith("python -m pip install ")
        ]
    else:
        matches = [step for step in job["steps"] if step.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def assert_lane_versions(documents):
    """Check every lane's own image and actual install arguments."""
    for workflow, lane, image_path, step_name in LANES:
        job = documents[workflow]["jobs"][lane]
        if image_path:
            image = job
            for key in image_path:
                image = image[key]
            if lane == "device-server":
                assert image == "${{ matrix.server.image }}", (workflow, lane, "server")
                assert job["strategy"]["matrix"]["server"] == [
                    {"version": SERVER_VERSION, "image": QDRANT_IMAGE},
                    DEVICE_COMPAT_SERVER,
                ], (workflow, lane, "matrix")
                version_step = next(
                    step
                    for step in job["steps"]
                    if step.get("name") == "Assert the running Qdrant Server version"
                )
                assert version_step["env"]["EXPECTED_QDRANT_VERSION"] == (
                    "${{ matrix.server.version }}"
                )
            else:
                assert image == QDRANT_IMAGE, (workflow, lane, "server")
        arguments = shlex.split(install_step(job, step_name)["run"], comments=True)
        assert [argument for argument in arguments if argument.startswith("qdrant-client")] == [
            CLIENT_PIN
        ], (workflow, lane, "client")


def test_server_lanes_use_reviewed_qdrant_pairs():
    """Version drift must fail CI instead of producing an ignored warning."""
    assert (
        QDRANT_IMAGE
        == "qdrant/qdrant@sha256:d18c8e5061be3eaea2f53fbee1f0bf0382dc59cbb9b7112e41ffcc937ec14220"
    )
    documents = workflows()
    assert_lane_versions(documents)
    assert documents["soak"]["env"]["PYTHONWARNINGS"] == "error:Qdrant client version:UserWarning"


def test_controlled_lock_resolves_and_the_lane_installed_it():
    """The controlled lock, the resolved lock and the installed client must agree."""
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    assert CLIENT_PIN in requirements
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    resolved = {
        package["version"] for package in lock["package"] if package["name"] == "qdrant-client"
    }
    assert resolved == {CONTROLLED_CLIENT}
    assert installed_version("qdrant-client") == CONTROLLED_CLIENT


def test_public_declarations_share_one_runtime_range():
    """The package and both manifests must declare the same client boundary."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = [
        item for item in project["project"]["dependencies"] if item.startswith("qdrant-client")
    ]
    assert declared == ["qdrant-client>=1.15,<2"]
    for name in ("plugin.yaml", "qdrant_memory/plugin.yaml"):
        manifest = YAML(typ="safe").load((ROOT / name).read_text(encoding="utf-8"))
        assert [
            item for item in manifest["python_dependencies"] if item.startswith("qdrant-client")
        ] == declared


def test_upgrade_rehearsal_pins_both_server_versions():
    """The patch-upgrade rehearsal keeps the previous image as a real fixture."""
    sys.path.insert(0, str(ROOT))
    try:
        from scripts import qdrant_upgrade_rehearsal as rehearsal
    finally:
        sys.path.pop(0)
    assert rehearsal.CURRENT_IMAGE == QDRANT_IMAGE
    assert rehearsal.PREVIOUS_IMAGE == PREVIOUS_SERVER_IMAGE
    assert rehearsal.PREVIOUS_IMAGE != rehearsal.CURRENT_IMAGE
    assert (rehearsal.PREVIOUS_VERSION, rehearsal.CURRENT_VERSION) == ("1.20.0", SERVER_VERSION)


@pytest.mark.parametrize(
    "index,component",
    [(index, "client") for index in range(len(LANES))] + [(index, "server") for index in range(3)],
)
def test_single_lane_drift_is_rejected(index, component):
    """Other lanes retaining the correct strings must never hide one bad pin."""
    documents = workflows()
    workflow, lane, image_path, step_name = LANES[index]
    job = documents[workflow]["jobs"][lane]
    if component == "client":
        step = install_step(job, step_name)
        step["run"] = step["run"].replace(CLIENT_PIN, "qdrant-client==1.15.1")
    else:
        target = job
        for key in image_path[:-1]:
            target = target[key]
        target[image_path[-1]] = "qdrant/qdrant@sha256:" + "0" * 64
    with pytest.raises(AssertionError):
        assert_lane_versions(documents)


@pytest.mark.parametrize("index,field", [(i, f) for i in (0, 1) for f in ("image", "version")])
def test_device_server_matrix_drift_is_rejected(index, field):
    """Refuse either an unexpected digest or an unexpected version in the device matrix."""
    documents = workflows()
    devices = documents["shared-security"]["jobs"]["device-server"]
    devices["strategy"]["matrix"]["server"][index][field] = "unexpected-value"
    with pytest.raises(AssertionError):
        assert_lane_versions(documents)


@pytest.mark.parametrize("reported", ["1.20.2", "1.20.1", None])
def test_device_server_version_probe_retries_startup(monkeypatch, capsys, tmp_path, reported):
    """Run the workflow probe through delayed startup, a version mismatch or timeout."""
    import time
    import urllib.request

    steps = workflows()["shared-security"]["jobs"]["device-server"]["steps"]
    _, _, probe = next(
        step["run"]
        for step in steps
        if step.get("name") == "Assert the running Qdrant Server version"
    ).partition("\n")
    probe = probe.rsplit("\nPY", 1)[0]
    probe_path = tmp_path / "probe.py"
    probe_path.write_text(probe, encoding="utf-8")
    elapsed = 0
    attempts = 0

    def sleep(seconds):
        nonlocal elapsed
        elapsed += seconds

    def open_probe(url, *, timeout):
        nonlocal attempts
        attempts += 1
        assert url == "http://127.0.0.1:6333/"
        assert 0 < timeout <= 2
        if attempts == 1 or reported is None:
            raise URLError("not listening yet")
        return io.StringIO(f'{{"version": "{reported}"}}')

    monkeypatch.setenv("EXPECTED_QDRANT_VERSION", "1.20.2")
    monkeypatch.setattr(time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(urllib.request, "urlopen", open_probe)
    if reported is None:
        with pytest.raises(SystemExit, match="within 60 seconds"):
            runpy.run_path(str(probe_path))
        assert elapsed == 60
    elif reported != "1.20.2":
        with pytest.raises(SystemExit, match="Expected Qdrant Server 1.20.2, got 1.20.1"):
            runpy.run_path(str(probe_path))
        assert attempts == 2
    else:
        runpy.run_path(str(probe_path))
        assert attempts == 2
        assert "Confirmed Qdrant Server 1.20.2" in capsys.readouterr().out
