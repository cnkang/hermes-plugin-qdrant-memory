"""The optional model environment must cover declared dependencies with verified pins."""

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def test_quality_lock_covers_runtime_remote_and_installer_requirements():
    """A missing dependency or artifact hash must not enter the model runner."""
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    text = (root / "requirements-quality.txt").read_text(encoding="utf-8")
    locked = {}
    for block in re.split(r"(?m)(?=^[A-Za-z0-9])", text):
        if not block.strip() or block.lstrip().startswith("#"):
            continue
        match = re.match(r"([A-Za-z0-9_.-]+)==([^\s;\\]+)", block)
        assert match is not None, block
        assert re.search(r"--hash=sha256:[0-9a-f]{64}\b", block), match[1]
        locked[canonicalize_name(match[1])] = match[2]
    inputs = (root / "requirements-quality.in").read_text(encoding="utf-8").splitlines()
    requirements = (
        project["project"]["dependencies"]
        + project["project"]["optional-dependencies"]["remote"]
        + project["build-system"]["requires"]
        + [line for line in inputs if line.strip() and not line.lstrip().startswith("#")]
    )
    for value in requirements:
        required = Requirement(value)
        assert canonicalize_name(required.name) in locked, required.name
        assert locked[canonicalize_name(required.name)] in required.specifier, required.name
