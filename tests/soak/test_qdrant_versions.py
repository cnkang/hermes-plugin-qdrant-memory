"""Keep the Docker test server and installed client on the reviewed release pair."""

import shlex
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from scripts.soak.prepare import QDRANT_IMAGE

ROOT = Path(__file__).resolve().parents[2]
CLIENT_PIN = "qdrant-client==1.20.0"
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
            assert image == QDRANT_IMAGE, (workflow, lane, "server")
        arguments = shlex.split(install_step(job, step_name)["run"], comments=True)
        assert [argument for argument in arguments if argument.startswith("qdrant-client")] == [
            CLIENT_PIN
        ], (workflow, lane, "client")


def test_server_lanes_use_one_reviewed_qdrant_pair():
    """Version drift must fail CI instead of producing an ignored warning."""
    assert (
        QDRANT_IMAGE
        == "qdrant/qdrant@sha256:9bfc89f9914fc14876a88c05270685921a55b8877b254a98bcc1cc2f0a424cd2"
    )
    documents = workflows()
    assert_lane_versions(documents)
    assert documents["soak"]["env"]["PYTHONWARNINGS"] == "error:Qdrant client version:UserWarning"


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
