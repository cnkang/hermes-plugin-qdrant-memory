"""Enforce the credential-free dependency input policy for pull requests."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_dependency_inputs.py"
SPEC = importlib.util.spec_from_file_location("check_dependency_inputs", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
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


def test_rejects_marked_pin_missing_from_snapshot(tmp_path):
    """A marker must not exempt a Linux-relevant pin from the hashed snapshot."""
    base = BASE + "typing-extensions==4.16.0 ; python_version >= '3'\n"
    with pytest.raises(MODULE.Failure):
        MODULE.check(write(tmp_path, base=base))


def test_accepts_windows_only_pin_without_snapshot_entry(tmp_path):
    """A Windows-only pin has no place in the Linux hashed snapshot."""
    MODULE.check(write(tmp_path))


def test_rejects_negated_windows_marker(tmp_path):
    """A negated Windows marker selects Linux and must keep a snapshot entry."""
    base = BASE + "requests-toolbelt==1.0.0 ; sys_platform != 'win32'\n"
    with pytest.raises(MODULE.Failure):
        MODULE.check(write(tmp_path, base=base))


def test_rejects_disjunctive_windows_marker(tmp_path):
    """A marker that also selects Linux is not Windows-only."""
    base = BASE + "winreg==1.0 ; sys_platform == 'win32' or sys_platform == 'linux'\n"
    with pytest.raises(MODULE.Failure):
        MODULE.check(write(tmp_path, base=base))


def test_comment_with_backslash_does_not_swallow_next_pin(tmp_path):
    """A comment line never continues, so the next requirement is still checked."""
    base = "# pinned for the runtime \\\nevil==1.0\n"
    with pytest.raises(MODULE.Failure):
        MODULE.check(write(tmp_path, base=base))


def test_normalizes_package_names(tmp_path):
    """PEP 503 normalization lets the snapshot spell a name differently."""
    local = f"typing-extensions==4.16.0 \\\n    --hash=sha256:{Hash}\n"
    MODULE.check(write(tmp_path, base="typing_extensions==4.16.0\n", local=local))


def test_accepts_inline_comment(tmp_path):
    """A trailing comment does not change the pinned requirement."""
    MODULE.check(write(tmp_path, base="requests==2.32.3  # runtime pin\n"))


def test_rejects_duplicate_unmarked_pin(tmp_path):
    """Two unpinned entries for one name make the resolved version ambiguous."""
    base = "requests==2.32.3\nrequests==2.32.4\n"
    with pytest.raises(MODULE.Failure):
        MODULE.check(write(tmp_path, base=base))


def test_repository_inputs_satisfy_policy():
    """The committed dependency inputs pass the same policy CI enforces."""
    MODULE.check(Path(__file__).resolve().parents[1])
