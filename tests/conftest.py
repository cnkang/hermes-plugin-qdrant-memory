"""Tests import the actual Hermes host, never fake its provider ABC."""

import sys
import sysconfig
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if not sys.flags.isolated:
    sys.path.insert(0, str(ROOT))


def pytest_sessionstart(session):
    """Require isolated acceptance to exercise the installed distribution."""
    if sys.flags.isolated:
        import qdrant_memory

        origin = Path(qdrant_memory.__file__).resolve()
        installed_roots = {
            Path(sysconfig.get_path(key)).resolve() for key in ("purelib", "platlib")
        }
        if not any(origin.is_relative_to(root) for root in installed_roots):
            raise pytest.UsageError(
                "Isolated acceptance must import qdrant_memory from installed site-packages"
            )


def pytest_addoption(parser):
    """Pass live-service settings explicitly through Hermes's hermetic runner."""
    parser.addoption("--qdrant-test-url", default=None)
    parser.addoption("--qdrant-test-container", default=None)
    parser.addoption("--qdrant-cloud-config", default=None)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Bind every test to a temporary profile so user state is never modified."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
