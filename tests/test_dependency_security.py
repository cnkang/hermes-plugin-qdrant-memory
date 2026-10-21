"""Dependency snapshots must retain security fixes across install paths."""

import re
import tomllib
from pathlib import Path


def test_certifi_trust_store_fix_covers_every_dependency_lock():
    """Keep GHSA-6q48-6c7w-9gw2 fixed in Runtime and hashed CPU installs."""
    root = Path(__file__).resolve().parents[1]
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    certifi = next(package for package in lock["package"] if package["name"] == "certifi")
    runtime = re.search(
        r"^certifi==([0-9.]+)$", (root / "requirements.txt").read_text(encoding="utf-8"), re.M
    )
    cpu = re.search(
        r"^certifi==([0-9.]+)(.*?)(?=^\S|\Z)",
        (root / "requirements-local.txt").read_text(encoding="utf-8"),
        re.M | re.S,
    )
    quality = re.search(
        r"^certifi==([0-9.]+)(.*?)(?=^\S|\Z)",
        (root / "requirements-quality.txt").read_text(encoding="utf-8"),
        re.M | re.S,
    )
    assert runtime is not None and cpu is not None and quality is not None
    versions = {
        "uv.lock": certifi["version"],
        "requirements.txt": runtime[1],
        "requirements-local.txt": cpu[1],
        "requirements-quality.txt": quality[1],
    }
    for manifest, version in versions.items():
        assert tuple(map(int, version.split("."))) >= (2026, 10, 19), manifest
    assert len(set(versions.values())) == 1, versions
    artifacts = [certifi["sdist"], *certifi["wheels"]]
    assert set(re.findall(r"--hash=(sha256:[0-9a-f]{64})", quality[2])) == {
        artifact["hash"] for artifact in artifacts
    }
    assert set(re.findall(r"--hash=(sha256:[0-9a-f]{64})", cpu[2])) == {
        artifact["hash"] for artifact in artifacts
    }
