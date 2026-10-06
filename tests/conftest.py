"""Tests import the actual Hermes host, never fake its provider ABC."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Bind every test to a temporary profile so user state is never modified."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
