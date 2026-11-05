"""Report and enforce the Python interpreter selected by the CI matrix."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_SNAPSHOT = r"""
import importlib.metadata as metadata
import json
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

try:
    distribution = metadata.distribution(DISTRIBUTION)
    normalized_name = re.sub(r"[-_.]+", "_", DISTRIBUTION).lower()
    search_roots = dict.fromkeys(
        (sysconfig.get_path("purelib"), sysconfig.get_path("platlib"))
    )
    metadata_candidates = [
        candidate
        for root in search_roots
        for candidate in Path(root).glob(f"{normalized_name}-*.dist-info/METADATA")
    ]
    distribution_metadata = (
        str(metadata_candidates[0].resolve()) if metadata_candidates else None
    )
    distribution_location = (
        str(Path(distribution_metadata).parent.parent)
        if distribution_metadata
        else str(Path(distribution.locate_file("")).resolve())
    )
except metadata.PackageNotFoundError:
    distribution_location = None
    distribution_metadata = None

pip = subprocess.run(
    [sys.executable, "-m", "pip", "--version"],
    check=False,
    capture_output=True,
    text=True,
)
uv_path = shutil.which("uv")
uv_version = None
if uv_path:
    uv = subprocess.run([uv_path, "--version"], check=False, capture_output=True, text=True)
    uv_version = uv.stdout.strip() if uv.returncode == 0 else "unavailable"

print(json.dumps({
    "executable": sys.executable,
    "version": sys.version,
    "version_info": list(sys.version_info[:2]),
    "implementation": platform.python_implementation(),
    "prefix": sys.prefix,
    "purelib_install_target": sysconfig.get_path("purelib"),
    "scripts_install_target": sysconfig.get_path("scripts"),
    "pip": pip.stdout.strip() if pip.returncode == 0 else "not installed",
    "uv_executable": uv_path,
    "uv_version": uv_version,
    "distribution_location": distribution_location,
    "distribution_metadata": distribution_metadata,
}))
"""


def _snapshot(executable: str, distribution: str) -> dict[str, object]:
    """Read interpreter and install-target details without printing environment values."""
    script = "DISTRIBUTION = " + repr(distribution) + "\n" + _SNAPSHOT
    result = subprocess.run(
        [executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONPATH": ""},
    )
    return json.loads(result.stdout)


def _version_pair(snapshot: dict[str, object]) -> tuple[int, int]:
    """Return the major/minor pair reported by the interpreter."""
    parts = snapshot["version_info"]
    return int(parts[0]), int(parts[1])


def _expected_pair(value: str) -> tuple[int, int]:
    """Parse the matrix's major.minor Python version."""
    parts = value.split(".")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError("Expected Python version must use MAJOR.MINOR")
    return int(parts[0]), int(parts[1])


def _is_within(root: Path, candidate: Path) -> bool:
    """Check that a reported install target belongs to the requested environment."""
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _python_in(environment: Path) -> Path:
    """Find the generated environment's interpreter on POSIX or Windows."""
    candidates = (environment / "Scripts" / "python.exe", environment / "bin" / "python")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"No Python interpreter found in {environment}")


def main(argv: list[str] | None = None) -> int:
    """Print both selected and installed interpreter details, then enforce the matrix."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", required=True, help="Expected Python major.minor")
    parser.add_argument("--environment", type=Path)
    parser.add_argument("--distribution", default="hermes-plugin-qdrant-memory")
    parser.add_argument("--require-distribution", action="store_true")
    args = parser.parse_args(argv)
    expected = _expected_pair(args.expected)
    setup = _snapshot(sys.executable, args.distribution)
    target_executable = (
        _python_in(args.environment).absolute()
        if args.environment is not None
        else Path(sys.executable).absolute()
    )
    target = _snapshot(str(target_executable), args.distribution)
    print(json.dumps({"setup_python": setup}, sort_keys=True))
    print(json.dumps({"runtime_environment": target}, sort_keys=True))
    for label, snapshot in (("setup Python", setup), ("runtime environment", target)):
        if _version_pair(snapshot) != expected:
            raise SystemExit(f"{label} is Python {_version_pair(snapshot)}, expected {expected}")
    if args.require_distribution and not target["distribution_location"]:
        raise SystemExit(f"{args.distribution} is not installed in {target_executable}")
    if args.environment is not None and args.require_distribution:
        metadata_path = target["distribution_metadata"]
        if not metadata_path or not _is_within(args.environment, Path(str(metadata_path))):
            raise SystemExit(f"{args.distribution} metadata is outside {args.environment}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
