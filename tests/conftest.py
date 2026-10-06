"""Tests import the actual Hermes host, never fake its provider ABC."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def pytest_addoption(parser):
    """Pass live-service settings explicitly through Hermes's hermetic runner."""
    parser.addoption("--qdrant-test-url", default=None)
    parser.addoption("--qdrant-test-container", default=None)
    parser.addoption("--qdrant-cloud-config", default=None)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Bind every test to a temporary profile so user state is never modified."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
