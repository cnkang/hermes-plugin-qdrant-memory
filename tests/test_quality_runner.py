"""Evaluation observations must not change the system under test or hide failures."""

import json
import runpy
import subprocess
from contextlib import nullcontext
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


def test_project_replay_uses_committed_receipt_after_pending_observation(tmp_path, monkeypatch):
    """Intermediate Pi receipts cannot invalidate a later committed event replay."""
    module = runner()
    scope = module["evaluate"].__globals__
    recorder = SimpleNamespace(extractions={}, latencies=[], errors={})
    manager = SimpleNamespace(shutdown_all=lambda: None)
    monkeypatch.setitem(scope, "start_host", lambda *_args: (object(), manager))
    monkeypatch.setitem(scope, "tls_owner", lambda *_args: nullcontext(("url", "certificate")))
    monkeypatch.setitem(scope, "read_quality", lambda *_args: ({}, [], 0, {}))
    monkeypatch.setitem(scope, "remote_query", lambda *_args: [])
    monkeypatch.setitem(scope, "score_run", lambda *_args: {})

    def driver(*_args, input, **_kwargs):
        """Retain both observations of the same durable event, like the Pi driver."""
        case = json.loads(input)[0]
        event = {"event_id": case["id"]}
        captures = [
            {
                "case_id": case["id"],
                "event": event,
                "status": status,
                "receipt": {"status": status, "event_id": case["id"]},
            }
            for status in ("PENDING", "COMMITTED")
        ]
        return subprocess.CompletedProcess(
            [], 0, json.dumps({"captures": captures, "history_leak": False}), ""
        )

    monkeypatch.setattr(scope["subprocess"], "run", driver)
    replays = []

    async def committed(_url, _token, event, **_kwargs):
        """The real endpoint now returns the terminal receipt for the same event."""
        replays.append(event["event_id"])
        return {"status": "COMMITTED", "event_id": event["event_id"]}

    monkeypatch.setitem(scope, "send_event", committed)
    data = {"cases": [{"id": route, "route": route} for route in ("project", "pi")]}
    tokens = {name: name for name in ("PROJECT", "READER", "PI", "SECOND")}
    result = module["evaluate"](data, tmp_path, tmp_path, recorder, tokens)
    assert result["replay_safe"] is True
    assert result["restart_safe"] is True
    assert result["restart_model_calls"] == 0
    assert replays == ["project", "project"]
