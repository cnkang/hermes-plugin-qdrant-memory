"""Evaluation observations must not change the system under test or hide failures."""

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import httpx


def runner():
    """Load this checkout's evaluator independently of ambient scripts packages."""
    return runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts" / "evaluate_owner_lifecycle.py")
    )


def test_recording_error_preserves_successful_model_result():
    """A changed/redacted input cannot convert successful inference into provider failure."""
    module = runner()
    result = SimpleNamespace(parsed={"memories": []})
    observer = module["RecordingLLM"](
        SimpleNamespace(complete_structured=lambda **kwargs: result),
        [{"id": "case", "user": "original"}],
    )
    assert (
        observer.complete_structured(
            purpose="qdrant-memory.extract", input=[{"text": json.dumps({"user": "redacted"})}]
        )
        is result
    )
    assert not observer.errors
    assert observer.recording_errors == {"KeyError": 1}


def test_model_probe_failure_produces_redacted_fail_artifact(tmp_path, monkeypatch, capsys):
    """Unavailable Ollama still returns a machine-readable FAIL without unsafe messages."""
    module = runner()
    output = tmp_path / "evidence.json"
    monkeypatch.setattr(
        "sys.argv", ["evaluate", "--adapter-root", str(tmp_path), "--output", str(output)]
    )
    monkeypatch.delenv("QM_QUALITY_UNUSED_KEY", raising=False)

    def fail(**kwargs):
        raise httpx.ConnectError("synthetic-secret-must-not-leak")

    monkeypatch.setattr(module["main"].__globals__["httpx"], "Client", fail)
    assert module["main"]() == 1
    report = json.loads(output.read_text())
    assert report["status"] == "FAIL"
    assert report["failure_type"] == "ConnectError"
    assert "synthetic-secret-must-not-leak" not in output.read_text() + capsys.readouterr().out
