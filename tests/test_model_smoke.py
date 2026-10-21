"""Smoke results must remain separate from full model-quality acceptance."""

import json
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("healthy", [True, False, None])
def test_smoke_cli_never_certifies_quality(tmp_path, monkeypatch, capsys, healthy):
    """The actual CLI fails a broken round trip and labels healthy runs only SMOKE_PASS."""
    module = runpy.run_path(str(ROOT / "scripts/smoke_owner_lifecycle.py"))
    scope = module["main"].__globals__
    output = tmp_path / "smoke.json"
    monkeypatch.setattr(
        "sys.argv", ["smoke", "--adapter-root", str(tmp_path), "--output", str(output)]
    )
    checks = {name: True for name in module["CHECKS"]}
    checks["restart_durable"] = healthy
    if healthy is None:
        checks.clear()
    monkeypatch.setitem(scope, "run_smoke", lambda _adapter, _profile=None: {"checks": checks})
    result = module["main"]()
    report = json.loads(output.read_text())
    assert result == (0 if healthy else 1)
    assert report["status"] == ("SMOKE_PASS" if healthy else "FAIL")
    assert report["baseline_executed"] is False
    assert report["quality_evaluated"] is False
    assert report["evaluation_mode"] == "smoke"
    assert json.loads(capsys.readouterr().out)["status"] != "PASS"


def test_smoke_cli_redacts_failure_messages(tmp_path, monkeypatch, capsys):
    """The failure artifact keeps source stages without private exception text."""
    module = runpy.run_path(str(ROOT / "scripts/smoke_owner_lifecycle.py"))
    output = tmp_path / "smoke.json"
    monkeypatch.setattr(
        "sys.argv", ["smoke", "--adapter-root", str(tmp_path), "--output", str(output)]
    )

    def fail(_adapter, _profile=None):
        raise ValueError("synthetic-private-key")

    monkeypatch.setitem(module["main"].__globals__, "run_smoke", fail)
    assert module["main"]() == 1
    assert "synthetic-private-key" not in output.read_text() + capsys.readouterr().out
    assert json.loads(output.read_text())["failure_type"] == "ValueError"


@pytest.mark.parametrize("failure", [None, "empty", "exception"])
def test_repeated_smoke_requires_every_fresh_lifecycle(tmp_path, monkeypatch, failure):
    """A failed repetition stops the test; a later success cannot hide it."""
    module = runpy.run_path(str(ROOT / "scripts/smoke_owner_lifecycle.py"))
    output = tmp_path / "smoke.json"
    monkeypatch.setattr(
        "sys.argv",
        ["smoke", "--adapter-root", str(tmp_path), "--output", str(output), "--repetitions", "5"],
    )
    calls = []

    def attempt(_adapter, _profile=None):
        calls.append(True)
        if len(calls) == 2 and failure == "exception":
            raise RuntimeError("synthetic-private-key")
        checks = {name: True for name in module["CHECKS"]}
        if len(calls) == 2 and failure == "empty":
            checks["pi_capture"] = False
        return {"checks": checks}

    monkeypatch.setitem(module["main"].__globals__, "run_smoke", attempt)
    assert module["main"]() == (1 if failure else 0)
    report = json.loads(output.read_text())
    assert len(calls) == (2 if failure else 5)
    assert report["requested_repetitions"] == 5
    assert len(report["runs"]) == (1 if failure == "exception" else len(calls))
    assert report["status"] == ("FAIL" if failure else "SMOKE_PASS")
    assert report["quality_evaluated"] is False
    assert "synthetic-private-key" not in output.read_text()


@pytest.mark.parametrize("repetitions", ["0", "6"])
def test_smoke_repetitions_are_bounded(tmp_path, monkeypatch, repetitions):
    """Reject an invalid repetition budget before starting a model or Owner."""
    module = runpy.run_path(str(ROOT / "scripts/smoke_owner_lifecycle.py"))
    monkeypatch.setattr(
        "sys.argv",
        [
            "smoke",
            "--adapter-root",
            str(tmp_path),
            "--output",
            str(tmp_path / "out.json"),
            "--repetitions",
            repetitions,
        ],
    )
    with pytest.raises(SystemExit) as caught:
        module["main"]()
    assert caught.value.code == 2


@pytest.mark.parametrize("profile", ["smoke-qwen06", "smoke-qwen08", "smoke-qwen17", "smoke-qwen2"])
def test_smoke_cli_selects_one_frozen_comparison_profile(tmp_path, monkeypatch, profile):
    """A manual comparison uses exactly the requested local profile and existing checks."""
    module = runpy.run_path(str(ROOT / "scripts/smoke_owner_lifecycle.py"))
    output = tmp_path / "smoke.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "smoke",
            "--adapter-root",
            str(tmp_path),
            "--output",
            str(output),
            "--model-profile",
            profile,
        ],
    )
    observed = []

    def attempt(_adapter, selected=None):
        observed.append(selected)
        return {"checks": {name: True for name in module["CHECKS"]}}

    monkeypatch.setitem(module["main"].__globals__, "run_smoke", attempt)
    assert module["main"]() == 0
    assert observed == [profile]
    assert json.loads(output.read_text())["model_profile"] == profile


@pytest.mark.parametrize("profile", ["reference", "openrouter-free", "unknown"])
def test_smoke_cli_rejects_non_smoke_profiles(tmp_path, monkeypatch, profile):
    """A local smoke selection cannot start external or reference model routing."""
    module = runpy.run_path(str(ROOT / "scripts/smoke_owner_lifecycle.py"))
    monkeypatch.setattr(
        "sys.argv",
        [
            "smoke",
            "--adapter-root",
            str(tmp_path),
            "--output",
            str(tmp_path / "out.json"),
            "--model-profile",
            profile,
        ],
    )
    with pytest.raises(SystemExit) as caught:
        module["main"]()
    assert caught.value.code == 2
