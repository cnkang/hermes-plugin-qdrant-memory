"""Evaluation observations must not change the system under test or hide failures."""

import json
import runpy
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


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


@pytest.mark.parametrize("only", [False, True])
def test_model_probe_failure_produces_redacted_fail_artifact(tmp_path, monkeypatch, capsys, only):
    """Unavailable Ollama still returns a machine-readable FAIL without unsafe messages."""
    module = runner()
    output = tmp_path / "evidence.json"
    arguments = ["evaluate", "--adapter-root", str(tmp_path), "--output", str(output)]
    if only:
        arguments += ["--scenarios", "synthetic.json", "--scenarios-only"]
    monkeypatch.setattr("sys.argv", arguments)
    monkeypatch.delenv("QM_QUALITY_UNUSED_KEY", raising=False)

    def fail(**kwargs):
        raise httpx.ConnectError("synthetic-secret-must-not-leak")

    monkeypatch.setattr(module["main"].__globals__["httpx"], "Client", fail)
    assert module["main"]() == 1
    report = json.loads(output.read_text())
    assert report["status"] == "FAIL"
    assert report["failure_type"] == "ConnectError"
    assert report["baseline_executed"] is False
    assert report["evaluation_mode"] == ("mutations-only" if only else "full")
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
    monkeypatch.setitem(scope, "score_contexts", lambda *_args, **_kwargs: {})

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


@pytest.mark.parametrize("only", [False, True])
@pytest.mark.parametrize("problem", ["none", "mutation", "recording", "provider", "gate_exception"])
def test_diagnostic_success_cannot_certify_baseline(tmp_path, monkeypatch, capsys, only, problem):
    """The actual runner separates diagnostic success and preserves failure gates."""
    module = runner()
    globals_ = module["main"].__globals__
    output = tmp_path / "evidence.json"
    extension = tmp_path / "integrations" / "pi" / "owner-memory.mjs"
    extension.parent.mkdir(parents=True)
    extension.write_text("// Synthetic adapter identity for runner-only contracts.\n")
    scenarios = globals_["ROOT"] / "tests" / "quality" / "lifecycle-mutations-v1.json"
    arguments = [
        "evaluate",
        "--adapter-root",
        str(tmp_path),
        "--output",
        str(output),
        "--scenarios",
        str(scenarios),
    ]
    if only:
        arguments.append("--scenarios-only")
    monkeypatch.setattr("sys.argv", arguments)
    monkeypatch.delenv("QM_QUALITY_UNUSED_KEY", raising=False)

    class InstalledModelProbe:
        """Provide the installed-model inventory without accessing a model server."""

        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, _url):
            return SimpleNamespace(
                json=lambda: {
                    "models": [
                        {"name": name, "digest": "synthetic-model-digest"}
                        for name in (globals_["MODEL"], globals_["EMBEDDING"])
                    ]
                }
            )

    baseline_calls = []

    def baseline(data, _adapter, _home, recorder, _tokens):
        """Supply complete observations to the real scorer through its I/O boundary."""
        baseline_calls.append(True)
        for row in data["cases"]:
            recorder.extractions[row["id"]] = [
                " ".join(group[0] for group in fact["terms"]) for fact in row["expected"]
            ]
        return {
            "extractions": dict(recorder.extractions),
            "retrievals": {row["id"]: [row["expected"]] for row in data["queries"]},
            "scope_leaks": 0,
            "provider_failures": 0,
            "replay_safe": True,
            "restart_safe": True,
            "restart_raw_equal": True,
            "restart_changed_fields": [],
            "restart_model_calls": 0,
            "post_restart_quality": {} if problem == "gate_exception" else {"status": "PASS"},
            "context_quality": {"status": "PASS"},
            "retrieval_latency": {},
        }

    def mutations(_core, _scenario, _home, recorder, _tokens, _adapter):
        """Supply scenario outcomes without substituting runner status decisions."""
        if problem == "recording":
            recorder.recording_errors["KeyError"] += 1
        if problem == "provider":
            recorder.errors["TimeoutError"] += 1
        if problem == "gate_exception" and only:
            raise RuntimeError("synthetic scenario failure")
        return {"status": "FAIL" if problem == "mutation" else "PASS"}

    monkeypatch.setattr(globals_["httpx"], "Client", InstalledModelProbe)
    monkeypatch.setitem(globals_, "OwnerLLM", lambda *_args: SimpleNamespace(close=lambda: None))
    monkeypatch.setitem(globals_, "evaluate", baseline)
    monkeypatch.setattr(
        globals_["runpy"], "run_path", lambda _path: {"evaluate_mutations": mutations}
    )
    exit_code = module["main"]()
    report = json.loads(output.read_text())
    emitted = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert bool(baseline_calls) is not only
    assert report["baseline_executed"] is not only
    assert report["evaluation_mode"] == ("mutations-only" if only else "full")
    expected = "FAIL" if problem != "none" else "DIAGNOSTIC_PASS" if only else "PASS"
    assert report["status"] == expected
    assert exit_code == (1 if problem != "none" else 0)
    assert emitted["status"] == expected
    if only:
        assert report["cases"] == report["queries"] == 0
