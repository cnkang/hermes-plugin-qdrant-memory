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
    monkeypatch.setitem(scope, "run_smoke", lambda _adapter: {"checks": checks})
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

    def fail(_adapter):
        raise ValueError("synthetic-private-key")

    monkeypatch.setitem(module["main"].__globals__, "run_smoke", fail)
    assert module["main"]() == 1
    assert "synthetic-private-key" not in output.read_text() + capsys.readouterr().out
    assert json.loads(output.read_text())["failure_type"] == "ValueError"
