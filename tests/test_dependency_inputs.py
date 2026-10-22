"""Enforce the credential-free dependency input policy for pull requests."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_dependency_inputs.py"
SPEC = importlib.util.spec_from_file_location("check_dependency_inputs", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

Hash = "a" * 64
BASE = "requests==2.32.3\nnumpy==2.4.6 ; python_full_version < '3.12'\npywin32==312 ; sys_platform == 'win32'\n"
LOCAL = f"""requests==2.32.3 \\
    --hash=sha256:{Hash}
numpy==2.4.6 \\
    --hash=sha256:{Hash}
"""


def write(tmp_path, base=BASE, local=LOCAL):
    """Write one dependency input pair and return the repository root."""
    (tmp_path / "requirements.txt").write_text(base, encoding="utf-8")
    (tmp_path / "requirements-local.txt").write_text(local, encoding="utf-8")
    return tmp_path


def test_accepts_exact_pins_and_hashes(tmp_path):
    """A pinned base file and its hashed snapshot pass the policy."""
    MODULE.check(write(tmp_path))


@pytest.mark.parametrize(
    "requirement",
    [
        "requests>=2.32.3\n",
        "requests~=2.32\n",
        "requests\n",
        "requests @ https://example.invalid/requests.whl\n",
        "git+https://example.invalid/requests.git\n",
        "-r other.txt\n",
        "--index-url https://example.invalid/simple\n",
    ],
)
def test_rejects_ranges_sources_and_options(tmp_path, requirement):
    """Every non-exact requirement form fails with a policy violation."""
    with pytest.raises(MODULE.Failure):
        MODULE.check(write(tmp_path, base=requirement))


def test_rejects_unhashed_local_entry(tmp_path):
    """A snapshot entry without a hash cannot be verified."""
    with pytest.raises(MODULE.Failure):
        MODULE.check(write(tmp_path, local="requests==2.32.3\n"))


def test_rejects_short_hash(tmp_path):
    """A truncated hash is not a sha256 value."""
    local = "requests==2.32.3 --hash=sha256:abc\n"
    with pytest.raises(MODULE.Failure):
        MODULE.check(write(tmp_path, local=local))


def test_rejects_version_mismatch(tmp_path):
    """The hashed snapshot must repeat the base version."""
    local = LOCAL.replace("requests==2.32.3", "requests==2.32.4")
    with pytest.raises(MODULE.Failure):
        MODULE.check(write(tmp_path, local=local))


def test_rejects_missing_unmarked_pin(tmp_path):
    """An unmarked base pin must appear in the hashed snapshot."""
    entry = f"requests==2.32.3 \\\n    --hash=sha256:{Hash}\n"
    with pytest.raises(MODULE.Failure):
        MODULE.check(write(tmp_path, local=LOCAL.replace(entry, "")))
