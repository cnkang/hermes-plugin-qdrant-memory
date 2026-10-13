"""Fail closed on conflicting artifacts and verify safe PyPI upload retries."""

import argparse
import hashlib
import json
import os
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path


def main():
    """Compare every built artifact with PyPI's immutable release digest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require", action="store_true")
    args = parser.parse_args()
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    url = f"https://pypi.org/pypi/{project['name']}/{project['version']}/json"
    for attempt in range(12 if args.require else 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                release = json.load(response)
            break
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            if args.require and attempt < 11:
                time.sleep(5)
                continue
            if args.require:
                raise RuntimeError("PyPI release is unavailable") from exc
            release = None
    exists = release is not None
    if exists:
        remote = {item["filename"]: item for item in release["urls"]}
        artifacts = list(Path("dist").glob("*.whl")) + list(Path("dist").glob("*.tar.gz"))
        assert artifacts, "No artifacts to compare"
        for artifact in artifacts:
            item = remote.get(artifact.name)
            assert item and not item["yanked"], f"Missing/yanked artifact: {artifact.name}"
            assert item["digests"]["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest(), (
                f"Conflicting immutable artifact: {artifact.name}"
            )
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"exists={str(exists).lower()}\n")
    print(f"PyPI {project['name']} {project['version']}: exists={exists}")


if __name__ == "__main__":
    main()
