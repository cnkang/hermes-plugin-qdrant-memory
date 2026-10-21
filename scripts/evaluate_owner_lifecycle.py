"""Opt-in anonymous multi-turn Hermes/Pi evaluation against installed local models."""

import argparse
import hashlib
import inspect
import json
import os
import re
import runpy
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import anyio
import httpx
import uvicorn

from qdrant_memory.config import DEFAULTS, embedding_config, merge
from qdrant_memory.embedding import HTTPEmbeddingProvider
from qdrant_memory.extraction import MEMORY_SCHEMA, Extractor
from qdrant_memory.owner_lifecycle import OwnerLifecycle
from qdrant_memory.owner_llm import OwnerLLM
from qdrant_memory.quality_metrics import (
    durable_snapshot,
    latency_summary,
    matches,
    score_contexts,
    score_run,
    validate_gold,
)
from qdrant_memory.remote_client import call_remote, send_event
from qdrant_memory.remote_gateway import create_gateway

ROOT = Path(__file__).resolve().parents[1]
MODEL = "qwen3:8b"
EMBEDDING = "qwen3-embedding:4b"


def model_profile(name):
    """Keep hosted and routed model results distinct from the original reference."""
    definitions = json.loads((ROOT / "tests/quality/model-profiles.json").read_text())
    selected = definitions["profiles"][name]
    local = "http://127.0.0.1:" + ("11434" if name == "reference" else "19134")
    external = name == "openrouter-free"
    return {
        "name": name,
        "ollama_url": local,
        "local_models": selected["local_models"],
        "embedding": {
            "provider": "ollama",
            "model": selected["embedding_model"],
            "dimensions": selected["embedding_dimensions"],
            "base_url": local,
            "timeout_seconds": 30,
        },
        "llm": {
            "mode": "override",
            "provider": "openai-compatible",
            "model": "openrouter/free" if external else selected["generation_model"],
            "base_url": "https://openrouter.ai/api/v1" if external else local + "/v1",
            "max_output_tokens": 2048,
            "api_key_env": "OPENROUTER_API_KEY" if external else "QM_QUALITY_UNUSED_KEY",
            **(
                {"reasoning_effort": selected["reasoning_effort"]}
                if "reasoning_effort" in selected
                else {}
            ),
        },
    }


def select_installed_models(profile, models):
    """Reject missing models and changed hosted model tags before inference."""
    expected = profile["local_models"]
    installed = {row["name"]: row["digest"] for row in models if row["name"] in expected}
    if set(installed) != set(expected):
        raise ValueError("Evaluation requires the explicit installed local models")
    for name, checksum in expected.items():
        if checksum and installed[name].removeprefix("sha256:") != checksum:
            raise ValueError("Installed model digest differs from the frozen profile")
    return installed


def digest(value):
    """Fingerprint evaluation inputs without exposing their contents."""
    return hashlib.sha256(value).hexdigest()


class RecordingLLM:
    """Observe actual model output while delegating all decisions to the existing facade."""

    def __init__(self, delegate, cases, profile=None):
        """Bind a real facade and the anonymous case IDs being observed."""
        self.delegate = delegate
        self.model_profile = profile or model_profile("reference")
        self.case_ids = {case["user"]: case["id"] for case in cases}
        self.extractions, self.latencies, self.errors = {}, [], Counter()
        self.recording_errors = Counter()
        self.lock = threading.Lock()
        self.provider_models, self.http_statuses = Counter(), Counter()
        self.request_lock, self.last_request = threading.Lock(), 0.0
        hooks = getattr(getattr(delegate, "client", None), "event_hooks", None)
        if isinstance(hooks, dict):
            hooks.setdefault("response", []).append(self.observe_response)

    def observe_response(self, response):
        """Record public model IDs and status codes, never provider bodies or credentials."""
        with self.lock:
            self.http_statuses[response.status_code] += 1
        if response.status_code != 200:
            return
        try:
            response.read()
            name = response.json().get("model")
            key = os.environ.get(self.model_profile["llm"]["api_key_env"])
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", name)
                or (key and key in name)
            ):
                raise ValueError("Provider model identity is unavailable or unsafe")
        except Exception as exc:
            with self.lock:
                self.recording_errors[type(exc).__name__] += 1
        else:
            with self.lock:
                self.provider_models[name] += 1

    def complete_structured(self, **kwargs):
        """Record extraction observations and latency, preserving facade failures."""
        started = time.monotonic()
        try:
            if self.model_profile["name"] == "openrouter-free":
                # Keep sequential free-tier requests below 20 requests/minute.
                # There is no paid fallback or automatic retry after a denial.
                with self.request_lock:
                    time.sleep(max(0, 3.1 - (time.monotonic() - self.last_request)))
                    self.last_request = time.monotonic()
            result = self.delegate.complete_structured(**kwargs)
        except Exception as exc:
            with self.lock:
                self.errors[type(exc).__name__] += 1
            raise
        finally:
            with self.lock:
                self.latencies.append((time.monotonic() - started) * 1000)
        if kwargs.get("purpose") == "qdrant-memory.extract":
            try:
                event = json.loads(kwargs["input"][0]["text"])
                key = self.case_ids[event["user"]]
                rows = [row["text"] for row in result.parsed["memories"]]
            except Exception as exc:
                with self.lock:
                    self.recording_errors[type(exc).__name__] += 1
            else:
                with self.lock:
                    self.extractions[key] = rows
        return result


def policy(tokens):
    """Authorize two native devices and independently scoped Pi credentials."""
    specs = {
        "NATIVE": ("hermes", "native-a", "native-private", "private", "write"),
        "SECOND": ("hermes", "native-b", "native-private", "private", "write"),
        "PI": ("pi", "pi-a", "pi-private", "private", "write"),
        "PROJECT": ("pi", "pi-project", "alpha", "project", "write"),
        "READER": ("pi", "pi-a", "alpha", "project", "read"),
    }
    clients = []
    for name, token in tokens.items():
        logical, device, space, visibility, permission = specs[name]
        grant = {
            "token_sha256": digest(token.encode()),
            "principal": device,
            "owner_id": "evaluation-user",
            "logical_agent_id": logical,
            "device_id": device,
            "space_id": space,
            "visibility": visibility,
            "permission": permission,
        }
        if name == "PROJECT":
            grant["capture_rule_id"] = "anonymous-evaluation-project-opt-in"
        clients.append(grant)
    return {"clients": clients}


def start_host(home, policy_path, recorder):
    """Start the actual Hermes provider and MemoryManager with the existing Runtime."""
    from agent.memory_manager import MemoryManager

    from qdrant_memory.provider import QdrantMemoryProvider

    cfg = merge(
        DEFAULTS,
        {
            "scope": {"user_id": "evaluation-user", "agent_id": None},
            "qdrant": {"path": str(home / "vectors")},
            "embedding": recorder.model_profile["embedding"],
            "native_owner": {
                "enabled": True,
                "policy": str(policy_path),
                "port": 0,
                "private_token_env": "QM_QUALITY_NATIVE",
            },
        },
    )
    embedder = HTTPEmbeddingProvider(embedding_config(cfg), secret_reader=lambda _: None)
    provider = QdrantMemoryProvider(
        plugin_context=SimpleNamespace(llm=recorder), embedder=embedder, overrides=cfg
    )
    try:
        provider.initialize("native-0", hermes_home=str(home))
    except BaseException:
        provider.shutdown()
        raise
    manager = MemoryManager()
    manager.add_provider(provider)
    return provider, manager


@contextmanager
def tls_owner(provider, home):
    """Bind a temporary loopback TLS server; no public listener or Cloud credentials."""
    cert, key = home / "cert.pem", home / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(64)
        authority = f"127.0.0.1:{sock.getsockname()[1]}"
        owner = provider._native_owner.owner
        app = create_gateway(owner, allowed_hosts=[authority], lifecycle=OwnerLifecycle(owner))
        service = uvicorn.Server(
            uvicorn.Config(
                app,
                ssl_certfile=str(cert),
                ssl_keyfile=str(key),
                proxy_headers=False,
                access_log=False,
                log_level="critical",
                timeout_graceful_shutdown=35,
            )
        )
        thread = threading.Thread(target=service.run, kwargs={"sockets": [sock]})
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not service.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not service.started:
                raise RuntimeError("Evaluation TLS listener failed startup")
            yield f"https://{authority}/mcp", cert
        finally:
            service.should_exit = True
            thread.join(timeout=40)
            if thread.is_alive() and sys.exc_info()[0] is None:
                raise RuntimeError("Evaluation Owner did not drain")


def remote_query(url, certificate, token, query):
    """Retrieve only through official HTTPS MCP and the selected grant."""

    async def call():
        return await call_remote(
            url, token, "qdrant_memory_search", {"query": query}, ca_file=certificate
        )

    return anyio.run(call)["memories"]


def event_replay(url, certificate, token, event):
    """Replay an original Pi event through the existing authorized event endpoint."""

    async def call():
        return await send_event(url, token, event, ca_file=certificate)

    return anyio.run(call)


def pi_capture(adapter, home, rows, url, certificate, token, recall_token):
    """Require actual Pi session hooks and an acknowledged capture for every turn."""
    home.mkdir()
    env = {
        **os.environ,
        "QDRANT_MEMORY_OWNER_URL": url,
        "QDRANT_MEMORY_CA_FILE": str(certificate),
        "QDRANT_MEMORY_RUNTIME_PYTHON": sys.executable,
        "QDRANT_MEMORY_REMOTE_TOKEN": token,
        "QDRANT_MEMORY_RECALL_TOKEN": recall_token,
        "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    result = subprocess.run(
        ["node", str(ROOT / "scripts" / "pi_lifecycle_quality.mjs"), str(adapter), str(home)],
        input=json.dumps(rows),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode:
        raise RuntimeError("Actual Pi quality driver failed")
    observed = json.loads(result.stdout.splitlines()[-1])
    committed = {row["case_id"] for row in observed["captures"] if row["status"] == "COMMITTED"}
    if committed != {row["id"] for row in rows} or observed["history_leak"]:
        raise RuntimeError("Pi capture incomplete or shared recall persisted in history")
    return observed


def read_quality(data, url, cert, tokens):
    """Measure annotated ranking and cross-agent private leakage via MCP."""
    facts = validate_gold(data)
    routes = {fact["id"]: case["route"] for case in data["cases"] for fact in case["expected"]}
    credentials = {"native": tokens["SECOND"], "pi": tokens["PI"], "project": tokens["READER"]}
    ranked, latencies, leaks, snapshots = {}, [], 0, {}
    for query in data["queries"]:
        started = time.monotonic()
        hits = remote_query(url, cert, credentials[query["route"]], query["query"])
        latencies.append((time.monotonic() - started) * 1000)
        snapshots[query["id"]] = durable_snapshot(hits)
        found = []
        for hit in hits:
            matching = [key for key, fact in facts.items() if matches(hit["text"], fact)]
            found.append(matching[0] if len(matching) == 1 else None)
            leaks += any(routes[key] != query["route"] for key in matching)
        ranked[query["id"]] = found
        # A different logical private Agent must not return the requested private fact.
        if query["route"] != "project":
            other = tokens["PI"] if query["route"] == "native" else tokens["SECOND"]
            denied = remote_query(url, cert, other, query["query"])
            leaks += sum(matches(hit["text"], facts[query["expected"]]) for hit in denied)
    return ranked, latencies, leaks, snapshots


def evaluate(data, adapter, home, recorder, tokens):
    """Run actual multi-turn capture, retrieval, replay and persistent-owner restart."""
    policy_path = home / "policy.json"
    document = policy(tokens)
    reader_grant = next(
        row
        for row in document["clients"]
        if row["token_sha256"] == digest(tokens["READER"].encode())
    )
    policy_path.write_text(json.dumps(document), encoding="utf-8")
    policy_path.chmod(0o600)
    provider, manager = start_host(home, policy_path, recorder)
    baseline, captured, replay_safe = None, None, False
    context_observations = []
    try:
        for index, row in enumerate(case for case in data["cases"] if case["route"] == "native"):
            provider.on_session_switch(row["session"])
            provider.on_turn_start(index + 1, row["user"], session_id=row["session"])
            manager.sync_all(row["user"], "ack", session_id=row["session"])
            if (
                not manager.flush_pending(timeout=10)
                or not provider.wait_idle(120)
                or provider._native_owner.owner.blocked
            ):
                raise RuntimeError("Native lifecycle failed or recovery gate blocked")
        with tls_owner(provider, home) as (url, cert):
            for route, token in (("project", tokens["PROJECT"]), ("pi", tokens["PI"])):
                observed = pi_capture(
                    adapter,
                    home / route,
                    [row for row in data["cases"] if row["route"] == route],
                    url,
                    cert,
                    token,
                    tokens["READER"],
                )
                if route == "project":
                    captured = next(
                        row for row in observed["captures"] if row["status"] == "COMMITTED"
                    )
                context_observations.extend(observed.get("contexts", []))
            ranked, latencies, leaks, snapshots = read_quality(data, url, cert, tokens)
            before = len(recorder.latencies)
            replayed = event_replay(url, cert, tokens["PROJECT"], captured["event"])
            replay_safe = replayed == captured["receipt"] and len(recorder.latencies) == before
            baseline = remote_query(url, cert, tokens["SECOND"], "Cedar database")
    finally:
        manager.shutdown_all()
    calls_before_restart = len(recorder.latencies)
    provider, manager = start_host(home, policy_path, recorder)
    try:
        with tls_owner(provider, home) as (url, cert):
            restored = remote_query(url, cert, tokens["SECOND"], "Cedar database")
            receipt = event_replay(url, cert, tokens["PROJECT"], captured["event"])
            after_ranked, after_latency, after_leaks, after_snapshots = read_quality(
                data, url, cert, tokens
            )
            latencies.extend(after_latency)
            leaks += after_leaks
            restart_safe = (
                snapshots == after_snapshots
                and durable_snapshot(restored) == durable_snapshot(baseline)
                and receipt == captured["receipt"]
                and len(recorder.latencies) == calls_before_restart
            )
            # Preserve raw comparison diagnostics without writing memory text.
            left = {row["id"]: row for row in baseline}
            right = {row["id"]: row for row in restored}
            changed_fields = sorted(
                {
                    key
                    for identity in left.keys() & right.keys()
                    for key in left[identity].keys() | right[identity].keys()
                    if left[identity].get(key) != right[identity].get(key)
                }
                | ({"missing_or_added_id"} if left.keys() != right.keys() else set())
            )
    finally:
        manager.shutdown_all()
    return {
        "extractions": recorder.extractions,
        "context_quality": score_contexts(
            data,
            context_observations,
            space_id=reader_grant["space_id"],
            visibility=reader_grant["visibility"],
        ),
        "retrievals": ranked,
        "scope_leaks": leaks,
        "provider_failures": sum(recorder.errors.values()),
        "replay_safe": replay_safe,
        "restart_safe": restart_safe,
        "restart_raw_equal": restored == baseline,
        "restart_changed_fields": changed_fields,
        "restart_model_calls": len(recorder.latencies) - calls_before_restart,
        "post_restart_quality": score_run(
            data,
            {
                "extractions": recorder.extractions,
                "retrievals": after_ranked,
                "scope_leaks": after_leaks,
                "provider_failures": sum(recorder.errors.values()),
                "replay_safe": replay_safe,
                "restart_safe": restart_safe,
            },
        ),
        "retrieval_latency": latency_summary(latencies),
    }


def main():
    """Run only installed local models and write a new, redacted evidence artifact."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--model-profile",
        choices=("reference", "hosted-ollama", "hosted-qwen35", "openrouter-free"),
        default="reference",
        help="Model identity and endpoint selection; never a paid fallback",
    )
    parser.add_argument("--scenarios", type=Path, help="Optional frozen mutation/isolation corpus")
    parser.add_argument(
        "--scenarios-only",
        action="store_true",
        help="Diagnostic mutation run; does not execute or certify the v1 baseline",
    )
    args = parser.parse_args()
    if args.scenarios_only and not args.scenarios:
        parser.error("--scenarios-only requires --scenarios")
    dataset_bytes = (ROOT / "tests" / "quality" / "lifecycle-v1.json").read_bytes()
    data = json.loads(dataset_bytes)
    validate_gold(data)
    if args.output.exists():
        raise FileExistsError("Choose a new evaluation output path")
    llm, recorder = None, None
    tokens = {key: secrets.token_hex(32) for key in ("NATIVE", "SECOND", "PI", "PROJECT", "READER")}
    report = {
        "status": "FAIL",
        "baseline_executed": False,
        "evaluation_mode": "mutations-only" if args.scenarios_only else "full",
        "dataset": data["version"],
        "dataset_sha256": digest(dataset_bytes),
        "models": {},
        "model_profile": args.model_profile,
        "cases": 0 if args.scenarios_only else len(data["cases"]),
        "queries": 0 if args.scenarios_only else len(data["queries"]),
        "prompt_sha256": digest(inspect.getsource(Extractor.extract).encode()),
        "schema_sha256": digest(json.dumps(MEMORY_SCHEMA, sort_keys=True).encode()),
        "source_sha256": {
            str(path.relative_to(ROOT)): digest(path.read_bytes())
            for path in [
                ROOT / "scripts" / "evaluate_owner_lifecycle.py",
                ROOT / "scripts" / "pi_lifecycle_quality.mjs",
                ROOT / "tests" / "quality" / "model-profiles.json",
                *sorted((ROOT / "scripts").glob("evaluate_owner_mutations.py")),
                *sorted((ROOT / "qdrant_memory").glob("*.py")),
            ]
        },
        "assistant": "deterministic Pi ack; real Owner LLM and embeddings",
        "limitations": "Concept-annotated synthetic dataset; not user UI or production accuracy",
    }
    try:
        if os.environ.get("QM_QUALITY_UNUSED_KEY"):
            raise ValueError("Local quality evaluation refuses supplied API credentials")
        profile = model_profile(args.model_profile)
        if args.model_profile == "openrouter-free" and not os.environ.get("OPENROUTER_API_KEY"):
            raise ValueError("Explicit OpenRouter test credential required")
        with httpx.Client(trust_env=False, timeout=5) as http:
            models = http.get(profile["ollama_url"] + "/api/tags").json()["models"]
        report["models"] = select_installed_models(profile, models)
        report["generation_route"] = profile["llm"]["model"]
        report["generation_reasoning_effort"] = profile["llm"].get(
            "reasoning_effort", "provider-default"
        )
        report["embedding_dimensions"] = profile["embedding"]["dimensions"]
        report["pi_extension_sha256"] = digest(
            (args.adapter_root / "integrations" / "pi" / "owner-memory.mjs").read_bytes()
        )
        llm = OwnerLLM(profile["llm"])
        recorder = RecordingLLM(llm, data["cases"], profile)
        from agent.secret_scope import reset_secret_scope, set_secret_scope

        with tempfile.TemporaryDirectory(prefix="qm-model-quality-") as directory:
            home = Path(directory).resolve()
            env = {"QM_QUALITY_" + key: value for key, value in tokens.items()}
            with patch.dict(os.environ, env):
                scoped = set_secret_scope(env, profile_home=str(home))
                try:
                    evidence = None
                    if not args.scenarios_only:
                        evidence = evaluate(
                            data, args.adapter_root.resolve(), home, recorder, tokens
                        )
                        report["baseline_executed"] = True
                    if args.scenarios:
                        scenario_bytes = args.scenarios.read_bytes()
                        scenario = json.loads(scenario_bytes)
                        validate_gold(scenario)
                        report["scenarios_version"] = scenario["version"]
                        report["scenarios_sha256"] = digest(scenario_bytes)
                        report["scenario_cases"] = len(scenario["cases"])
                        recorder.case_ids.update(
                            {row["user"]: row["id"] for row in scenario["cases"]}
                        )
                        extension = runpy.run_path(
                            str(ROOT / "scripts" / "evaluate_owner_mutations.py")
                        )
                        core = SimpleNamespace(
                            **{
                                name: globals()[name]
                                for name in (
                                    "start_host",
                                    "tls_owner",
                                    "call_remote",
                                    "remote_query",
                                    "pi_capture",
                                    "digest",
                                    "policy",
                                )
                            }
                        )
                        report["mutation_scenarios"] = extension["evaluate_mutations"](
                            core, scenario, home, recorder, tokens, args.adapter_root.resolve()
                        )
                        if evidence is not None:
                            evidence["extractions"] = {
                                key: value
                                for key, value in recorder.extractions.items()
                                if key in {row["id"] for row in data["cases"]}
                            }
                finally:
                    reset_secret_scope(scoped)
            if evidence is not None:
                report.update(score_run(data, evidence))
                report.update(
                    {
                        key: evidence[key]
                        for key in (
                            "scope_leaks",
                            "provider_failures",
                            "replay_safe",
                            "restart_safe",
                            "restart_raw_equal",
                            "restart_changed_fields",
                            "restart_model_calls",
                            "post_restart_quality",
                            "context_quality",
                            "retrieval_latency",
                        )
                    }
                )
            else:
                report["status"] = (
                    "DIAGNOSTIC_PASS"
                    if report["mutation_scenarios"]["status"] == "PASS"
                    else "FAIL"
                )
            report["provider_failures"] = sum(recorder.errors.values())
            if (
                (evidence is not None and report["post_restart_quality"]["status"] != "PASS")
                or (evidence is not None and report["context_quality"]["status"] != "PASS")
                or recorder.recording_errors
                or recorder.errors
                or (args.scenarios and report["mutation_scenarios"]["status"] != "PASS")
            ):
                report["status"] = "FAIL"
    except Exception as exc:
        report["status"] = "FAIL"
        report["failure_type"] = type(exc).__name__
        report["failure_location"] = [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(exc.__traceback__)[-5:]
        ]
        report["failure_stages"] = [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(exc.__traceback__)
            if Path(frame.filename).name.startswith("evaluate_owner")
        ]
        pending, seen, causes = [exc], set(), []
        while pending and len(causes) < 20:
            cause = pending.pop()
            if id(cause) in seen:
                continue
            seen.add(id(cause))
            causes.append(
                {
                    "type": type(cause).__name__,
                    "http_status": getattr(getattr(cause, "response", None), "status_code", None),
                    "mcp_code": getattr(getattr(cause, "error", None), "code", None),
                }
            )
            if isinstance(cause, BaseExceptionGroup):
                pending.extend(cause.exceptions)
            for attribute in ("__cause__", "__context__"):
                if getattr(cause, attribute, None) is not None:
                    pending.append(getattr(cause, attribute))
        report["failure_causes"] = causes
    finally:
        if llm is not None:
            llm.close()
    report["model_latency"] = latency_summary(recorder.latencies if recorder else [])
    report["provider_error_types"] = dict(recorder.errors) if recorder else {}
    report["recording_error_types"] = dict(recorder.recording_errors) if recorder else {}
    report["observed_provider_models"] = dict(recorder.provider_models) if recorder else {}
    report["provider_http_statuses"] = dict(recorder.http_statuses) if recorder else {}
    if args.model_profile != "reference" and not report["observed_provider_models"]:
        report["status"] = "FAIL"
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] in {"PASS", "DIAGNOSTIC_PASS"} else 1


if __name__ == "__main__":
    sys.exit(main())
