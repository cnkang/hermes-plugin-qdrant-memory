"""Exercise real Owner model I/O and persistence without certifying model quality."""

import argparse
import json
import os
import runpy
import secrets
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
core = SimpleNamespace(**runpy.run_path(str(ROOT / "scripts/evaluate_owner_lifecycle.py")))
CHECKS = {
    "native_capture",
    "pi_capture",
    "authorized_recall",
    "private_isolation",
    "replay_safe",
    "restart_durable",
    "restart_no_model_calls",
    "provider_observed",
}
CASES = [
    {"id": "smoke-native", "session": "smoke-native", "user": "I prefer dark mode."},
    {
        "id": "smoke-project",
        "session": "smoke-project",
        "user": "This project's text editor is Vim.",
    },
]


def exercise(adapter, home, recorder, tokens):
    """Use actual Hermes and Pi callbacks, HTTPS MCP, and the existing Owner Runtime."""
    native, project = CASES
    policy_path = home / "policy.json"
    policy_path.write_text(json.dumps(core.policy(tokens)), encoding="utf-8")
    policy_path.chmod(0o600)
    provider, manager = core.start_host(home, policy_path, recorder)
    try:
        provider.on_session_switch(native["session"])
        provider.on_turn_start(1, native["user"], session_id=native["session"])
        manager.sync_all(native["user"], "ack", session_id=native["session"])
        if (
            not manager.flush_pending(timeout=10)
            or not provider.wait_idle(120)
            or provider._native_owner.owner.blocked
        ):
            raise RuntimeError("Native smoke capture failed")
        if not recorder.extractions.get(native["id"]):
            raise RuntimeError("Native smoke capture produced no memory")
        native_text = recorder.extractions[native["id"]][0]
        with core.tls_owner(provider, home) as (url, cert):
            captured = core.pi_capture(
                adapter, home / "pi", [project], url, cert, tokens["PROJECT"], tokens["READER"]
            )
            if not recorder.extractions.get(project["id"]):
                raise RuntimeError("Pi smoke capture produced no memory")
            project_text = recorder.extractions[project["id"]][0]
            receipt = captured["captures"][0]
            before = [
                core.remote_query(url, cert, tokens[key], text)
                for key, text in (("SECOND", native_text), ("READER", project_text))
            ]
            recall = all(
                any(hit["text"] == text for hit in hits)
                for hits, text in zip(before, (native_text, project_text), strict=True)
            )
            native_ids = {hit["id"] for hit in before[0]}
            isolated = not any(
                hit["id"] in native_ids
                for hit in core.remote_query(url, cert, tokens["PI"], native_text)
            )
            calls = len(recorder.latencies)
            replay = (
                core.event_replay(url, cert, tokens["PROJECT"], receipt["event"])
                == receipt["receipt"]
            )
            replay = replay and len(recorder.latencies) == calls
    finally:
        manager.shutdown_all()
    provider, manager = core.start_host(home, policy_path, recorder)
    try:
        with core.tls_owner(provider, home) as (url, cert):
            after = [
                core.remote_query(url, cert, tokens[key], text)
                for key, text in (("SECOND", native_text), ("READER", project_text))
            ]
            replay = (
                replay
                and core.event_replay(url, cert, tokens["PROJECT"], receipt["event"])
                == receipt["receipt"]
            )
    finally:
        manager.shutdown_all()
    return {
        "native_capture": bool(native_text),
        "pi_capture": receipt["status"] == "COMMITTED" and bool(project_text),
        "authorized_recall": recall,
        "private_isolation": isolated,
        "replay_safe": replay,
        "restart_durable": all(
            core.durable_snapshot(left) == core.durable_snapshot(right)
            for left, right in zip(before, after, strict=True)
        ),
        "restart_no_model_calls": len(recorder.latencies) == calls,
        "provider_observed": bool(recorder.provider_models)
        and not (recorder.errors or recorder.recording_errors or recorder.background_failures),
    }


def run_smoke(adapter, profile_name="smoke-ollama"):
    """Isolate synthetic credentials, vector data, models and host state for one run."""
    from agent.secret_scope import reset_secret_scope, set_secret_scope

    if os.environ.get("QM_QUALITY_UNUSED_KEY"):
        raise ValueError("Local smoke refuses supplied API credentials")
    profile = core.model_profile(profile_name)
    with core.httpx.Client(trust_env=False, timeout=5) as http:
        response = http.get(profile["ollama_url"] + "/api/tags")
        response.raise_for_status()
        models = core.select_installed_models(profile, response.json()["models"])
    llm = core.OwnerLLM(profile["llm"])
    recorder = core.RecordingLLM(llm, CASES, profile)
    tokens = {key: secrets.token_hex(32) for key in ("NATIVE", "SECOND", "PI", "PROJECT", "READER")}
    try:
        with tempfile.TemporaryDirectory(prefix="qm-model-smoke-") as directory:
            home = Path(directory).resolve()
            env = {"QM_QUALITY_" + key: value for key, value in tokens.items()}
            with patch.dict(os.environ, env):
                scoped = set_secret_scope(env, profile_home=str(home))
                try:
                    checks = exercise(adapter, home, recorder, tokens)
                finally:
                    reset_secret_scope(scoped)
        return {
            "checks": checks,
            "models": models,
            "model_latency": core.latency_summary(recorder.latencies),
            "observed_provider_models": dict(recorder.provider_models),
        }
    except Exception as exc:
        exc.smoke_background_failures = recorder.background_failures
        exc.smoke_observations = {
            "model_latency": core.latency_summary(recorder.latencies),
            "observed_provider_models": dict(recorder.provider_models),
            "provider_http_statuses": dict(recorder.http_statuses),
            "provider_error_types": dict(recorder.errors),
            "recording_error_types": dict(recorder.recording_errors),
            "capture_counts": {
                row["id"]: len(recorder.extractions.get(row["id"], [])) for row in CASES
            },
        }
        raise
    finally:
        llm.close()


def main():
    """Write an explicit smoke result; it can never be a quality PASS."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, choices=range(1, 6), default=1)
    parser.add_argument(
        "--model-profile",
        choices=[
            "smoke-ollama",
            "smoke-qwen06",
            "smoke-qwen08",
            "smoke-qwen17",
            "smoke-qwen2",
            "smoke-qwen4",
        ],
        default="smoke-ollama",
    )
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Choose a new smoke output path")
    report = {
        "status": "FAIL",
        "evaluation_mode": "smoke",
        "model_profile": args.model_profile,
        "smoke_cases_version": "owner-smoke-declarative-v2",
        "smoke_cases_sha256": core.digest(json.dumps(CASES, sort_keys=True).encode()),
        "baseline_executed": False,
        "quality_evaluated": False,
        "requested_repetitions": args.repetitions,
        "runs": [],
        "source_sha256": {
            str(path.relative_to(ROOT)): core.digest(path.read_bytes())
            for path in (
                Path(__file__),
                ROOT / "scripts/evaluate_owner_lifecycle.py",
                ROOT / "tests/quality/model-profiles.json",
            )
        },
    }
    try:
        # Check fresh Owner lifecycles and stop at the first failure.
        for _ in range(args.repetitions):
            evidence = run_smoke(args.adapter_root.resolve(), args.model_profile)
            report["runs"].append(evidence)
            checks = evidence["checks"]
            if set(checks) != CHECKS or not all(value is True for value in checks.values()):
                break
        else:
            report["status"] = "SMOKE_PASS"
        if args.repetitions == 1 and report["runs"]:
            report.update(report["runs"][0])
    except Exception as exc:
        report["failure_type"] = type(exc).__name__
        report.update(getattr(exc, "smoke_observations", {}))
        report["background_failures"] = getattr(exc, "smoke_background_failures", [])
        report["failure_stages"] = [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in core.traceback.extract_tb(exc.__traceback__)[-8:]
        ]
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2)
    print(json.dumps(report))
    return 0 if report["status"] == "SMOKE_PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
