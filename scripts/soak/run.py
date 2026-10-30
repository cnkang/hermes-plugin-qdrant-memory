"""Run one soak window end to end: prepare, load, restart, verify, report.

The entry point is deliberately small and every stage is recoverable: the
evidence stream is opened first, the environment is torn down last, and any
harness exception still produces a HARNESS_ERROR report instead of a silent pass.
"""

import argparse
import asyncio
import json
import os
import random
import sys
import time
import traceback
from pathlib import Path

from . import prepare
from .common import (
    PROFILE_SECONDS,
    SCENARIOS,
    SECRETS,
    Journal,
    RunIdentity,
    Stopwatch,
    collection_name,
    exit_code,
    new_run_id,
    overall_status,
    platform_summary,
    public_argv,
    utc_now,
    version_of,
    write_json,
    write_junit,
)
from .load import (
    SoakClient,
    Workload,
    probe_authorization,
    probe_cas,
    probe_rate_limit,
    probe_retry_contract,
)
from .stub import ProviderStub
from .supervisor import OwnerStartError, OwnerSupervisor
from .verify import (
    Evidence,
    QdrantEvidence,
    as_dicts,
    copy_ledger,
    missing_invariants,
    read_ledger,
    verify,
)

SAMPLE_INTERVAL = 15.0
RESTART_INTERVAL = {"quick": 45.0, "full": 120.0}
FAULT_INTERVAL = {"quick": 40.0, "full": 90.0}
PROBE_INTERVAL = {"quick": 30.0, "full": 120.0}
MAX_OWNER_LOG_BYTES = 8 * 1024 * 1024


def repository_sha():
    """Return the tested commit from the runner environment or the checkout."""
    from_env = os.environ.get("GITHUB_SHA", "").strip()
    if from_env:
        return from_env
    import subprocess

    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=20
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def owner_command(env):
    """Return the exact Owner command line for this environment."""
    allowed = [f"127.0.0.1:{env.ports['mcp']}"]
    if "client_proxy" in env.ports:
        allowed.append(f"127.0.0.1:{env.ports['client_proxy']}")
    command = [
        sys.executable,
        "-m",
        "qdrant_memory.standalone",
        "--home",
        str(env.home),
        "remote-serve",
        "--policy",
        "./policy.json",
        "--port",
        str(env.ports["owner_plain"]),
        "--local-port",
        str(env.ports["owner_local"]),
    ]
    for host in allowed:
        command += ["--allowed-host", host]
    if env.lifecycle:
        command.append("--enable-lifecycle")
    return command


def owner_environment(env):
    """Return the Owner environment: private CA trust and no inherited secrets."""
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "SSL_CERT_FILE": str(env.ca_file),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for key in ("TMPDIR", "LANG", "LC_ALL"):
        if key in os.environ:
            environment[key] = os.environ[key]
    if env.llm_key:
        # A real extraction model is optional; the key reaches the Owner only.
        environment["SOAK_OWNER_LLM_KEY"] = env.llm_key
    return environment


def readiness_probe(env, identity):
    """Answer readiness with one real authenticated MCP search."""
    import anyio

    from qdrant_memory.remote_client import call_remote

    token = env.tokens[identity]

    async def probe():
        return await call_remote(
            env.owner_endpoint,
            token,
            "qdrant_memory_search",
            {"query": "ready"},
            ca_file=env.ca_file,
        )

    try:
        result = anyio.run(probe)
    except Exception:  # noqa: BLE001 - any failure means "not ready yet"
        return False
    return isinstance(result, dict) and "memories" in result


def collection_exists(env):
    """Return whether the fixed cloud collection already exists."""
    import os

    from qdrant_client import QdrantClient

    previous = os.environ.get("SSL_CERT_FILE")
    os.environ.pop("SSL_CERT_FILE", None)
    try:
        client = QdrantClient(url=env.qdrant_admin_endpoint, api_key=env.api_key, timeout=30)
        try:
            return client.collection_exists(env.collection)
        finally:
            client.close()
    finally:
        if previous is not None:
            os.environ["SSL_CERT_FILE"] = previous


def ledger_metrics(home):
    """Read ledger counters and sizes without touching the Owner's connection."""
    import sqlite3

    path = Path(home) / "qdrant-memory" / "state.db"
    result = {"ledger_bytes": None, "wal_bytes": None, "pending_operations": None}
    if not path.exists():
        return result
    result["ledger_bytes"] = path.stat().st_size
    wal = Path(str(path) + "-wal")
    result["wal_bytes"] = wal.stat().st_size if wal.exists() else 0
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM operations WHERE status IN ('PENDING','FAILED')"
            ).fetchone()
            result["pending_operations"] = row[0] if row else None
        finally:
            connection.close()
    except sqlite3.Error:
        result["pending_operations"] = None
    return result


def qdrant_points(env):
    """Count points through the admin listener; never through the faulted path."""
    import os

    from qdrant_client import QdrantClient

    previous = os.environ.get("SSL_CERT_FILE")
    os.environ["SSL_CERT_FILE"] = str(env.ca_file)
    try:
        client = QdrantClient(url=env.qdrant_admin_endpoint, api_key=env.api_key, timeout=10)
        try:
            return client.count(env.collection, exact=True).count
        finally:
            client.close()
    except Exception:  # noqa: BLE001 - a metric sample may legitimately fail
        return None
    finally:
        if previous is None:
            os.environ.pop("SSL_CERT_FILE", None)
        else:
            os.environ["SSL_CERT_FILE"] = previous


class Runner:
    """Coordinate the workload, the fault driver and the evidence stream."""

    def __init__(self, args, identity, env, journal, supervisor):
        """Bind the prepared environment and the reporting identity."""
        self.args = args
        self.identity = identity
        self.env = env
        self.journal = journal
        self.supervisor = supervisor
        self.client = SoakClient(
            endpoint=env.owner_endpoint,
            ca_file=env.ca_file,
            tokens=env.tokens,
            journal=journal,
            outbox_seconds=float(args.outbox_seconds),
        )
        self.workload = Workload(self.client, journal, seed=args.seed, lifecycle=env.lifecycle)
        self.fatal = ""
        self.start_failures = 0
        self.client_fault_open = False
        self.client.path_fault = lambda: self.client_fault_open
        self.faults = []
        self.stop_at = None

    def record_fault(self, phase, fault, **extra):
        """Publish one fault-window boundary for later diagnosis."""
        if fault.startswith("client_"):
            self.client_fault_open = phase == "begin"
        entry = self.journal.record("fault", phase=phase, fault=fault, **extra)
        self.faults.append({"phase": phase, "fault": fault, **extra})
        return entry

    async def metrics_task(self, clock=time.monotonic):
        """Sample process and storage resources for the whole window."""
        while clock() < (self.stop_at or clock()):
            sample = self.supervisor.sample()
            record = {
                "rss_bytes": sample.rss_bytes,
                "file_descriptors": sample.file_descriptors,
                "cpu_seconds": sample.cpu_seconds,
                "alive": sample.alive,
                **ledger_metrics(self.env.home),
            }
            if int(clock()) % 60 < SAMPLE_INTERVAL:
                record["qdrant_points"] = qdrant_points(self.env)
            self.journal.record("metric", **record)
            await asyncio.sleep(SAMPLE_INTERVAL)

    async def probe_task(self, clock=time.monotonic):
        """Run the authorization and CAS probes at a bounded cadence."""
        await asyncio.sleep(min(10.0, self.args.target_seconds / 6))
        while clock() < (self.stop_at or clock()) and not self.fatal:
            try:
                await probe_authorization(self.client, self.journal)
                await probe_cas(self.client, self.journal)
                if self.args.scenario == "network":
                    await probe_retry_contract(
                        self.client,
                        self.journal,
                        tokens=self.env.tokens,
                        endpoint=self.env.owner_endpoint,
                        ca_file=self.env.ca_file,
                    )
            except Exception as exc:  # noqa: BLE001 - probes must not end the run
                self.journal.record(
                    "harness", event="probe_error", error={"type": type(exc).__name__}
                )
            await asyncio.sleep(PROBE_INTERVAL[self.args.profile])

    async def restart_task(self, clock=time.monotonic):
        """Restart the Owner on a fixed cadence while requests are in flight."""
        interval = RESTART_INTERVAL[self.args.profile]
        await asyncio.sleep(interval)
        index = 0
        while clock() < (self.stop_at or clock()) and not self.fatal:
            index += 1
            force = index % 3 == 0
            self.record_fault("begin", "owner_sigkill" if force else "owner_sigterm")
            try:
                result = await asyncio.to_thread(self.supervisor.restart, force=force)
            except OwnerStartError as exc:
                self.start_failures += 1
                self.fatal = "owner_restart_failed"
                self.record_fault("end", "owner_restart_failed", detail=str(exc)[:400])
                return
            self.record_fault(
                "end",
                "owner_sigkill" if force else "owner_sigterm",
                ready_seconds=result["ready_seconds"],
            )
            await asyncio.sleep(max(0.0, interval - result["ready_seconds"]))

    async def network_task(self, clock=time.monotonic):
        """Inject bounded transport faults on both sides of the Owner."""
        from .prepare import CLIENT_PROXY, QDRANT_PROXY, Toxic

        toxic = Toxic(self.env.ports["toxiproxy_api"], "127.0.0.1")
        interval = FAULT_INTERVAL[self.args.profile]
        plan = (
            (QDRANT_PROXY, "latency", {"latency": 400, "jitter": 100}, 8.0),
            (QDRANT_PROXY, "reset_peer", {"timeout": 0}, 6.0),
            (QDRANT_PROXY, "timeout", {"timeout": 2000}, 6.0),
            (CLIENT_PROXY, "latency", {"latency": 500, "jitter": 200}, 6.0),
            (CLIENT_PROXY, "disconnect", {}, 4.0),
            (CLIENT_PROXY, "reset_peer", {"timeout": 0}, 5.0),
        )
        await asyncio.sleep(min(15.0, self.args.target_seconds / 5))
        try:
            await self._fault_cycle(toxic, plan, interval, clock)
        finally:
            # A window never ends with a fault still applied.
            for proxy in (QDRANT_PROXY, CLIENT_PROXY):
                with contextlib_suppress():
                    toxic.clear(proxy)

    async def _fault_cycle(self, toxic, plan, interval, clock):
        """Apply each planned fault for a bounded hold and clear it again."""
        index = 0
        while clock() < (self.stop_at or clock()) - interval and not self.fatal:
            proxy, kind, attributes, hold = plan[index % len(plan)]
            index += 1
            self.record_fault("begin", f"{proxy}_{kind}")
            try:
                if kind == "disconnect":
                    await asyncio.to_thread(self._set_proxy, toxic, proxy, False)
                else:
                    toxic.add(proxy, kind, attributes)
                await asyncio.sleep(min(hold, max(1.0, self.args.target_seconds / 40)))
                if kind == "disconnect":
                    await asyncio.to_thread(self._set_proxy, toxic, proxy, True)
                else:
                    toxic.clear(proxy)
                await asyncio.sleep(min(8.0, max(2.0, self.args.target_seconds / 200)))
            except Exception as exc:  # noqa: BLE001 - a failed fault is evidence, not a crash
                self.journal.record(
                    "harness",
                    event="fault_error",
                    fault=f"{proxy}_{kind}",
                    error=_fault_detail(exc),
                )
                with contextlib_suppress():
                    toxic.clear(proxy)
                with contextlib_suppress():
                    await asyncio.to_thread(self._set_proxy, toxic, proxy, True)
            finally:
                self.record_fault("end", f"{proxy}_{kind}")
            await asyncio.sleep(max(0.0, interval / 4))

    @staticmethod
    def _set_proxy(toxic, name, enabled):
        """Enable or disable one toxiproxy proxy."""
        import httpx

        response = httpx.post(
            f"{toxic.api}/proxies/{name}",
            json={"enabled": enabled},
            timeout=10,
            trust_env=False,
        )
        response.raise_for_status()
        return response.json()

    async def lost_response_task(self, clock=time.monotonic):
        """Commit a write and drop its response, then replay the same identifier."""
        await asyncio.sleep(self.args.target_seconds / 3)
        for attempt in range(1, 4):
            if clock() >= (self.stop_at or clock()) - 30:
                return
            request_id = f"network-lost-response-{attempt}"
            text = (
                "Fact SOAK-lost-response-"
                + str(attempt)
                + ": the Owner answers a replay from its receipt."
            )
            try:
                dropped = await self.client.abandon_response(
                    "hermes-a", "add", {"text": text}, request_id=request_id
                )
            except Exception as exc:  # noqa: BLE001 - a cut connection is retried here
                self.journal.record(
                    "harness",
                    event="lost_response_retry",
                    attempt=attempt,
                    error={"type": type(exc).__name__},
                )
                await asyncio.sleep(3.0)
                continue
            replay = await self.client.call(
                "hermes-a",
                "add",
                {"text": text},
                op="add",
                request_id=request_id,
                text=text,
                phase="network",
            )
            self.journal.record(
                "probe",
                probe="lost_response",
                status="PASS"
                if dropped.get("status") == 200 and replay.ok and replay.action == "ADD"
                else "FAIL",
                http_status=dropped.get("status"),
                action=replay.action,
                record_id=replay.value.get("id"),
                text=text,
            )
            return
        self.journal.record(
            "probe",
            probe="lost_response",
            status="INCOMPLETE",
            detail="no connection survived the fault windows",
        )

    async def run(self):
        """Run the workload with its scenario driver until the window closes."""
        self.stop_at = time.monotonic() + self.args.target_seconds
        self.client.deadline = self.stop_at + 5.0
        tasks = [
            asyncio.create_task(self.metrics_task()),
            asyncio.create_task(self.probe_task()),
        ]
        if self.args.scenario == "restart":
            tasks.append(asyncio.create_task(self.restart_task()))
        if self.args.scenario == "network":
            tasks.append(asyncio.create_task(self.network_task()))
            tasks.append(asyncio.create_task(self.lost_response_task()))
        try:
            await self.workload.run(stop_at=self.stop_at, workers=self.args.workers)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.args.scenario == "network" and not self.fatal:
            await probe_rate_limit(self.client, self.journal)
        return {"fatal": self.fatal}


def _fault_detail(exc):
    """Keep a bounded, secret-free description of a failed fault injection."""
    response = getattr(exc, "response", None)
    detail = {"type": type(exc).__name__}
    if response is not None:
        detail["status"] = response.status_code
        detail["body"] = response.text[:200]
    return detail


class contextlib_suppress:
    """Swallow one cleanup failure without changing the run outcome."""

    def __enter__(self):
        """Do nothing before the body."""
        return self

    def __exit__(self, *exc_info):
        """Return True so the failure is ignored."""
        return True


def parse_args(argv=None):
    """Parse the harness command line."""
    parser = argparse.ArgumentParser(prog="soak-run")
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--profile", choices=sorted(PROFILE_SECONDS), default="quick")
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--target-seconds", type=float, default=0.0)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--platform-label", default="")
    parser.add_argument("--outbox-seconds", type=float, default=150.0)
    parser.add_argument("--no-lifecycle", action="store_true")
    parser.add_argument("--backend", choices=("local", "cloud"), default="local")
    parser.add_argument("--cloud-url", default=os.environ.get("SOAK_QDRANT_URL", ""))
    parser.add_argument("--cloud-api-key", default=os.environ.get("SOAK_QDRANT_API_KEY", ""))
    parser.add_argument("--cloud-collection", default=os.environ.get("SOAK_QDRANT_COLLECTION", ""))
    parser.add_argument("--owner-id", default="")
    parser.add_argument("--provider-port", type=int, default=0)
    parser.add_argument("--cloud-llm-url", default=os.environ.get("SOAK_LLM_BASE_URL", ""))
    parser.add_argument("--cloud-llm-model", default=os.environ.get("SOAK_LLM_MODEL", ""))
    parser.add_argument("--cloud-llm-key", default=os.environ.get("SOAK_LLM_API_KEY", ""))
    return parser.parse_args(argv)


def owner_identity(args, run_id):
    """Return the run-scoped scope identity that bounds every written record."""
    if args.owner_id:
        return args.owner_id
    suffix = run_id.rsplit("-", 1)[-1].lower()
    return "soak-" + "".join(character for character in suffix if character.isalnum())


def cloud_llm(args):
    """Return the optional real-model routing, or None to stay fully local."""
    if not args.cloud_llm_url or not args.cloud_llm_model:
        return None
    return {
        "base_url": args.cloud_llm_url.rstrip("/"),
        "model": args.cloud_llm_model,
        "key": args.cloud_llm_key,
    }


def build_identity(args, started_at):
    """Return the run identity recorded in every artifact."""
    os_name, machine, _ = platform_summary()
    target = int(args.target_seconds or PROFILE_SECONDS[args.profile])
    label = args.platform_label or f"{os_name.split()[0].lower()}-{machine}"
    return RunIdentity(
        run_id=args.run_id or new_run_id(args.scenario, args.profile, seed=args.seed or None),
        scenario=args.scenario,
        profile=args.profile,
        seed=args.seed,
        commit_sha=repository_sha(),
        runtime_version=version_of("hermes-plugin-qdrant-memory", "source"),
        mcp_version=version_of("mcp"),
        platform=os_name,
        architecture=machine,
        started_at=started_at,
        target_seconds=target,
        source=args.backend,
        platform_label=label,
    )


def main(argv=None):
    """Run one soak window and return a distinct exit code per status."""
    args = parse_args(argv)
    args.target_seconds = int(args.target_seconds or PROFILE_SECONDS[args.profile])
    if args.seed == 0:
        args.seed = random.SystemRandom().randrange(1, 2**31)
    args.report_dir = Path(args.report_dir).resolve()
    args.report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    started_at = utc_now()
    identity = build_identity(args, started_at)
    for value in (args.cloud_api_key, args.cloud_llm_key):
        SECRETS.register(value)
    journal = Journal(args.report_dir / "events.jsonl")
    journal.record("run_start", **identity.as_dict(), argv=public_argv(sys.argv[1:]))
    stopwatch = Stopwatch()
    duration = 0.0
    env = None
    supervisor = None
    stub = None
    harness_error = ""
    owner_starts = start_failures = 0
    cloud_cleanup = {}
    cleaned_local = {}
    result = {"status": "INCOMPLETE"}
    try:
        stub = ProviderStub(port=args.provider_port).start()
        owner_id = owner_identity(args, identity.run_id)
        llm = cloud_llm(args)
        if args.backend == "cloud":
            env = prepare.build_cloud_environment(
                workdir=args.workdir,
                collection=args.cloud_collection,
                provider_base_url=stub.base_url,
                scene=args.scenario,
                seed=args.seed,
                owner_id=owner_id,
                cloud_url=args.cloud_url,
                cloud_api_key=args.cloud_api_key,
                lifecycle=not args.no_lifecycle,
                llm=llm,
            )
        else:
            env = prepare.build_environment(
                workdir=args.workdir,
                collection=collection_name(identity.run_id),
                provider_base_url=stub.base_url,
                scene=args.scenario,
                seed=args.seed,
                lifecycle=not args.no_lifecycle,
                owner_id=owner_id,
            )
        if llm and llm.get("key"):
            SECRETS.register(llm["key"])
        journal.record("environment", **env.public())
        prepare.initialize_collection(
            env,
            cwd=env.home,
            existing=args.backend == "cloud" and collection_exists(env),
        )
        journal.record("collection_ready", collection=env.collection)
        supervisor = OwnerSupervisor(
            owner_command(env),
            cwd=env.home,
            env=owner_environment(env),
            log_path=env.owner_log,
            ready_probe=lambda: readiness_probe(env, "hermes-a"),
            journal=journal,
        )
        supervisor.start()
        runner = Runner(args, identity, env, journal, supervisor)
        asyncio.run(runner.run())
        # The measured window ends with the workload, not with the recovery drain.
        duration = stopwatch.stop()
        if runner.fatal:
            journal.record("harness", event="run_aborted", reason=runner.fatal)
        # Recovery flush: every unresolved write is replayed with its identifier.
        recovered = asyncio.run(runner.workload.flush_pending(runner.client, journal))
        journal.record("recovery_flush", recovered=recovered)
        start_failures = runner.start_failures
        owner_starts = supervisor.starts
        supervisor.stop()
    except BaseException as exc:  # noqa: BLE001 - every failure must be reported
        harness_error = f"{type(exc).__name__}: {exc}"
        journal.record(
            "harness",
            event="harness_error",
            error={"type": type(exc).__name__, "detail": str(exc)[:1000]},
            traceback=traceback.format_exc()[-4000:],
        )
        duration = stopwatch.elapsed()
    finally:
        if supervisor is not None:
            owner_starts = supervisor.starts
            with contextlib_suppress():
                supervisor.close()
        ledger_copy = {}
        if env is not None and env.home.exists():
            with contextlib_suppress():
                ledger_copy = copy_ledger(env.home, args.report_dir / "ledger")
        evidence = Evidence()
        if env is not None:
            try:
                evidence.points = QdrantEvidence(
                    env.qdrant_admin_endpoint, env.api_key, env.collection, env.ca_file
                ).read()
            except Exception as exc:  # noqa: BLE001 - missing state is INCOMPLETE, not PASS
                evidence.collected = False
                evidence.error = type(exc).__name__
            with contextlib_suppress():
                evidence.ledger = read_ledger(args.report_dir / "ledger", env.namespace)
                evidence.integrity = evidence.ledger.get("integrity", "missing")
            with contextlib_suppress():
                owner_log = Path(env.owner_log).read_text(encoding="utf-8", errors="replace")
                trimmed = owner_log[-MAX_OWNER_LOG_BYTES:]
                (args.report_dir / "owner.log").write_text(SECRETS.scrub(trimmed), encoding="utf-8")
            try:
                cleaned_local = prepare.teardown(env)
            except Exception as exc:  # noqa: BLE001 - teardown is evidence too
                cleaned_local = {"error": type(exc).__name__}
            journal.record("teardown", **cleaned_local)
            if args.backend == "cloud":
                try:
                    cleaned = prepare.delete_run_records(env)
                except Exception as exc:  # noqa: BLE001 - cleanup failure is evidence
                    cleaned = {"deleted": False, "error": type(exc).__name__}
                journal.record("cloud_cleanup", **cleaned)
                cloud_cleanup = cleaned
        with contextlib_suppress():
            stub.close()
        journal.record(
            "run_end", duration_seconds=round(duration, 3), harness_error=bool(harness_error)
        )
        rows = journal.rows()
        invariants, summary, _ = verify(
            rows,
            evidence,
            grants=env.grants if env else [],
            identity=identity,
            duration_seconds=duration,
            target_seconds=args.target_seconds,
            owner_restarts=supervisor.restarts if supervisor else 0,
            owner_starts=owner_starts,
            start_failures=start_failures,
        )
        missing = missing_invariants(invariants)
        status, reason = overall_status(
            invariants,
            duration_seconds=duration,
            target_seconds=args.target_seconds,
            harness_error=harness_error
            or ("" if evidence.collected else "QDrant state unavailable"),
        )
        if missing:
            status, reason = "INCOMPLETE", "Missing invariants: " + ", ".join(missing)
        if cleaned_local.get("error") or cleaned_local.get("leftover_containers"):
            status, reason = "FAIL", "CLEANUP_FAILED: this run's test services were not removed"
        if args.backend == "cloud" and not cloud_cleanup.get("deleted"):
            status, reason = "FAIL", "CLEANUP_FAILED: this run's cloud records were not removed"
        result["status"] = status
        report = {
            "identity": identity.as_dict(),
            "status": status,
            "reason": reason,
            "duration_seconds": round(duration, 3),
            "summary": summary,
            "invariants": as_dicts(invariants),
            "environment": env.public() if env else {},
            "ledger_copy": ledger_copy,
            "owner_log_bytes": (args.report_dir / "owner.log").stat().st_size
            if (args.report_dir / "owner.log").exists()
            else 0,
            "faults": len(runner.faults) if "runner" in locals() else 0,
            "harness_error": harness_error,
            "cloud_cleanup": cloud_cleanup,
            "artifacts": ["report.json", "events.jsonl", "metrics.json", "junit.xml", "owner.log"],
        }
        write_json(args.report_dir / "report.json", report)
        write_json(
            args.report_dir / "metrics.json",
            {
                "identity": identity.as_dict(),
                "summary": summary,
                "samples": [
                    {
                        key: row.get(key)
                        for key in (
                            "ts",
                            "rss_bytes",
                            "file_descriptors",
                            "cpu_seconds",
                            "ledger_bytes",
                            "wal_bytes",
                            "pending_operations",
                            "qdrant_points",
                        )
                    }
                    for row in rows
                    if row.get("kind") == "metric"
                ],
            },
        )
        write_junit(
            args.report_dir / "junit.xml",
            identity,
            duration,
            [
                {"name": item.name, "status": item.status, "detail": item.detail}
                for item in invariants
            ],
        )
        journal.close()
        print(
            json.dumps(
                {
                    "status": status,
                    "reason": reason,
                    "duration_seconds": round(duration, 3),
                    "summary": {
                        key: summary[key]
                        for key in (
                            "total_requests",
                            "successful_requests",
                            "unexpected_failures",
                            "permanent_write_losses",
                            "duplicate_records",
                            "owner_restarts",
                        )
                    },
                }
            )
        )
    return exit_code(result.get("status", "INCOMPLETE"))


if __name__ == "__main__":
    raise SystemExit(main())
