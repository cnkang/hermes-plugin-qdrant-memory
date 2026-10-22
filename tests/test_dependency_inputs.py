"""Enforce the credential-free dependency input policy for pull requests."""

import importlib.util
import sys
from pathlib import Path

import pytest
from packaging.markers import Marker

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
    base = "requests==2.32.3\npywin32==312 ; sys_platform == 'win32'\n"
    local = f"requests==2.32.3 \\\n    --hash=sha256:{Hash}\n"
    MODULE.check(write(tmp_path, base=base, local=local))


def test_rejects_negated_windows_marker(tmp_path):
    """A negated Windows marker selects Linux and must keep a snapshot entry."""
    base = BASE + "requests-toolbelt==1.0.0 ; sys_platform != 'win32'\n"
    with pytest.raises(MODULE.Failure, match="missing the hashed entry for requests-toolbelt"):
        MODULE.check(write(tmp_path, base=base))


def test_rejects_disjunctive_windows_marker(tmp_path):
    """A marker that also selects Linux is not Windows-only."""
    base = BASE + "winreg==1.0 ; sys_platform == 'win32' or sys_platform == 'linux'\n"
    with pytest.raises(MODULE.Failure, match="missing the hashed entry for winreg"):
        MODULE.check(write(tmp_path, base=base))


def test_quoted_windows_text_does_not_exempt_linux_dependency(tmp_path):
    """Windows text inside a literal must not bypass the Linux snapshot policy."""
    marker = '''os_name not in "sys_platform == 'win32'"'''
    assert Marker(marker).evaluate({"os_name": "posix", "sys_platform": "linux"})
    base = BASE + f"linux-required==1.0 ; {marker}\n"
    with pytest.raises(MODULE.Failure, match="missing the hashed entry for linux-required"):
        MODULE.check(write(tmp_path, base=base))


@pytest.mark.parametrize(
    "marker,expected",
    [
        ("sys_platform == 'win32'", True),
        ("'win32' == sys_platform", True),
        ("platform_system == 'Windows'", True),
        ("sys_platform == 'win32' and python_version >= '3.11'", True),
        ("sys_platform == 'win32' or platform_system == 'Windows'", True),
        ("sys_platform == 'win32' or sys_platform == 'darwin'", False),
        ("sys_platform != 'win32'", False),
        ("python_version >= '3.11'", False),
        ("sys_platform in 'win32,linux'", False),
        ("sys_platform == 'win32' or (os_name == 'posix' and python_version >= '9')", False),
        ("not a valid marker !", False),
        ("", False),
    ],
)
def test_windows_exemption_requires_structural_proof(marker, expected):
    """Only expressions proved false on every non-Windows platform qualify."""
    assert MODULE.windows_only([marker]) is expected


def test_comment_with_backslash_does_not_swallow_next_pin(tmp_path):
    """A comment line never continues, so the next requirement is still checked."""
    base = "# pinned for the runtime \\\nevil==1.0\n"
    root = write(tmp_path, base=base)
    assert "evil" in MODULE.parse_pins(root / "requirements.txt")
    with pytest.raises(MODULE.Failure, match="missing the hashed entry for evil"):
        MODULE.check(root)


def test_hashed_parser_keeps_pin_after_comment_with_backslash(tmp_path):
    """A comment line never continues in the hashed snapshot either."""
    local = f"# pinned for the runtime \\\nrequests==2.32.3 \\\n    --hash=sha256:{Hash}\n"
    root = write(tmp_path, local=local)
    assert MODULE.parse_hashed(root / "requirements-local.txt")["requests"] == "2.32.3"


def test_normalizes_package_names(tmp_path):
    """PEP 503 normalization lets the snapshot spell a name differently."""
    local = f"typing-extensions==4.16.0 \\\n    --hash=sha256:{Hash}\n"
    MODULE.check(write(tmp_path, base="typing_extensions==4.16.0\n", local=local))


def test_rejects_duplicate_normalized_snapshot_names(tmp_path):
    """Duplicate detection compares PEP 503 normalized names."""
    local = (
        f"typing-extensions==4.16.0 --hash=sha256:{Hash}\n"
        f"typing_extensions==4.16.0 --hash=sha256:{Hash}\n"
    )
    root = write(tmp_path, base="typing-extensions==4.16.0\n", local=local)
    with pytest.raises(MODULE.Failure, match="duplicate requirement: typing-extensions"):
        MODULE.check(root)


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
