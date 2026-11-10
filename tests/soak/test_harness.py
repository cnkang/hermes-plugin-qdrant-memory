"""Evidence, statistics and redaction contracts of the soak harness."""

import json
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

from scripts.soak.aggregate import aggregate, expected_legs, render_markdown, validate_report
from scripts.soak.common import (
    REQUIRED_INVARIANTS,
    SECRETS,
    Invariant,
    Journal,
    RunIdentity,
    Stopwatch,
    collection_name,
    exit_code,
    new_run_id,
    overall_status,
    percentile,
    public_argv,
    read_jsonl,
    trend,
    write_json,
    write_junit,
)


def test_journal_appends_durably_and_ignores_a_torn_line(tmp_path):
    """A crash mid-write must not lose the records already acknowledged."""
    path = tmp_path / "events.jsonl"
    with Journal(path) as journal:
        journal.record("op", request_id="one")
        journal.record("op", request_id="two")
    with open(path, "a", encoding="utf-8") as stream:
        stream.write('{"kind": "op", "request_id": "tr')
    rows = read_jsonl(path)
    assert [row["request_id"] for row in rows] == ["one", "two"]
    assert Journal(path).rows() == rows


def test_journal_serializes_writes_from_several_threads(tmp_path):
    """The supervisor journals from a worker thread while the loop writes."""
    import threading

    path = tmp_path / "events.jsonl"
    journal = Journal(path)

    def write(index):
        for attempt in range(20):
            journal.record("op", worker=index, attempt=attempt)

    threads = [threading.Thread(target=write, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    journal.close()
    rows = read_jsonl(path)
    assert len(rows) == 120
    assert all(set(row) >= {"kind", "worker", "attempt"} for row in rows)


def test_journal_scrubs_registered_and_token_shaped_values(tmp_path):
    """Credentials must never reach an artifact, even by accident."""
    token = SECRETS.register("soak-token-value-0123456789abcdef")
    path = tmp_path / "events.jsonl"
    with Journal(path) as journal:
        journal.record("op", note=f"used {token}", header="Authorization: Bearer abcdefgh")
    text = path.read_text(encoding="utf-8")
    assert token not in text
    assert "soak-token-value" not in text
    assert "abcdefgh" not in text
    commit = "0123456789abcdef0123456789abcdef01234567"
    with Journal(path) as journal:
        journal.record("op", commit_sha=commit, content_hash="sha256:" + "f" * 64)
    assert commit in path.read_text(encoding="utf-8")


def test_write_json_is_atomic_and_scrubbed(tmp_path):
    """A report is replaced whole, never left half-written."""
    path = tmp_path / "report.json"
    value = {"token": SECRETS.register("another-soak-token-0123456789"), "nested": [{"x": "y"}]}
    write_json(path, value)
    assert json.loads(path.read_text())["token"] == "<redacted>"
    assert not list(tmp_path.glob("*.tmp"))


def test_a_credential_never_reaches_the_recorded_command_line():
    """The harness records its own arguments, so a flag value must be masked."""
    argv = [
        "--backend",
        "cloud",
        "--cloud-api-key",
        "super-secret-value",
        "--cloud-llm-key=another-secret",
        "--cloud-llm-url",
        "https://llm.example",
    ]
    public = public_argv(argv)
    assert "super-secret-value" not in public
    assert "another-secret" not in public
    assert public[public.index("--cloud-api-key") + 1] == "<redacted>"
    assert "--cloud-llm-key=<redacted>" in public
    assert public[-1] == "https://llm.example"


def test_run_identity_and_collection_name_are_safe():
    """The run id identifies the window and stays a legal collection name."""
    run_id = new_run_id("restart", "quick", seed=7)
    assert run_id.startswith("soak-quick-restart-")
    assert collection_name(run_id).startswith("qm_soak_")
    assert collection_name(run_id).replace("_", "").isalnum()
    assert collection_name(run_id).lower() == collection_name(run_id)


def test_percentiles_and_trend_are_measured_not_guessed():
    """Latency percentiles use nearest rank; a trend reports its own slope."""
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert percentile(values, 0.5) == 5.0
    assert percentile(values, 0.95) == 10.0
    assert percentile([], 0.95) is None
    rising = trend([100, 110, 120, 130], unit_seconds=60)
    assert rising["slope_per_minute"] == pytest.approx(10.0)
    assert rising["change"] == pytest.approx(30.0)
    assert trend([5])["samples"] == 1


def test_overall_status_never_upgrades_missing_evidence():
    """PASS needs every invariant, a covered window and no harness error."""
    passed = [Invariant(name=name, status="PASS") for name in REQUIRED_INVARIANTS]
    status, _ = overall_status(passed, duration_seconds=480, target_seconds=480, harness_error=None)
    assert status == "PASS"
    failing = [*passed[:-1], Invariant(name=REQUIRED_INVARIANTS[-1], status="FAIL")]
    assert overall_status(failing, duration_seconds=480, target_seconds=480)[0] == "FAIL"
    assert overall_status(passed, duration_seconds=200, target_seconds=480)[0] == "INCOMPLETE"
    assert (
        overall_status(passed, duration_seconds=480, target_seconds=480, harness_error="boom")[0]
        == "HARNESS_ERROR"
    )
    duplicated = [
        Invariant(name=REQUIRED_INVARIANTS[0], status="PASS")
        for _ in range(len(REQUIRED_INVARIANTS))
    ]
    assert overall_status(duplicated, duration_seconds=480, target_seconds=480)[0] == "INCOMPLETE"
    short_set = passed[:-1]
    assert overall_status(short_set, duration_seconds=480, target_seconds=480)[0] == "INCOMPLETE"


def test_exit_codes_distinguish_every_status():
    """A caller must be able to tell a failure from missing evidence."""
    codes = {
        status: exit_code(status) for status in ("PASS", "FAIL", "INCOMPLETE", "HARNESS_ERROR")
    }
    assert len(set(codes.values())) == 4
    assert codes["PASS"] == 0


def test_junit_marks_every_non_pass_case(tmp_path):
    """The runner must see one failing test case per broken invariant."""
    identity = RunIdentity(
        run_id="soak-quick-steady-1",
        scenario="steady",
        profile="quick",
        seed=1,
        commit_sha="a" * 40,
        runtime_version="0.5.4",
        mcp_version="2.0.0",
        platform="Linux",
        architecture="x86_64",
        started_at="2026-10-30T00:00:00Z",
        target_seconds=480,
        platform_label="ubuntu-24.04",
    )
    path = write_junit(
        tmp_path / "junit.xml",
        identity,
        480.0,
        [
            {"name": "write_durability", "status": "PASS", "detail": ""},
            {"name": "ledger_integrity", "status": "FAIL", "detail": "pending work"},
        ],
    )
    # The test parses the file it just wrote; there is no untrusted input here.
    suite = ElementTree.fromstring(path.read_text(encoding="utf-8")).find("./testsuite")
    assert suite is not None
    assert suite.get("tests") == "2"
    assert suite.get("failures") == "1"
    assert suite.get("time") == "480.000"


def test_stopwatch_uses_a_monotonic_clock():
    """Effective duration must not depend on wall-clock changes."""
    ticks = iter([10.0, 12.5, 100.0, 500.0])
    watch = Stopwatch(clock=lambda: next(ticks))
    assert watch.elapsed() == pytest.approx(2.5)
    assert watch.stop() == pytest.approx(90.0)
    assert watch.stop() == pytest.approx(90.0)


def _report(tmp_path, *, status="PASS", commit="a" * 40, duration=480, target=480, deleted=True):
    """Write one complete runner artifact directory."""
    directory = tmp_path / "artifacts" / "leg"
    directory.mkdir(parents=True, exist_ok=True)
    summary: dict = {
        key: 0
        for key in (
            "total_requests",
            "successful_requests",
            "unexpected_failures",
            "permanent_write_losses",
            "duplicate_records",
            "owner_restarts",
        )
    }
    summary["latency_p50_p95_p99"] = {"p50": 1.0, "p95": 2.0, "p99": 3.0}
    summary["ledger_status"] = {"integrity": "ok"}
    payload = {
        "identity": {
            "commit_sha": commit,
            "platform_label": "ubuntu-24.04",
            "scenario": "steady",
            "target_seconds": target,
        },
        "status": status,
        "summary": summary,
        "invariants": [{"name": name, "status": "PASS"} for name in REQUIRED_INVARIANTS],
        "cloud_cleanup": {"deleted": deleted},
    }
    (directory / "report.json").write_text(json.dumps(payload), encoding="utf-8")
    (directory / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (directory / "junit.xml").write_text("<testsuites/>\n", encoding="utf-8")
    (directory / "metrics.json").write_text("{}\n", encoding="utf-8")
    return directory


def test_validate_report_rejects_a_short_window_a_wrong_commit_and_a_failed_leg(tmp_path):
    """A window that cannot support the acceptance is never reported as PASS."""
    directory = _report(tmp_path / "short", duration=300, target=300)
    status, reasons, _ = validate_report(directory, minimum_seconds=18000)
    assert status == "incomplete"
    assert any("cannot support" in reason for reason in reasons)
    mismatch = _report(tmp_path / "commit", commit="b" * 40)
    assert validate_report(mismatch, expected_commit="a" * 40)[0] == "incomplete"
    failed = _report(tmp_path / "failed", status="FAIL")
    assert validate_report(failed)[0] == "failed"


def test_validate_report_requires_every_artifact(tmp_path):
    """A missing evidence file makes the leg unverifiable."""
    directory = _report(tmp_path)
    (directory / "metrics.json").unlink()
    status, reasons, _ = validate_report(directory)
    assert status == "missing"
    assert "metrics.json" in reasons[0]


def test_aggregate_missing_legs_and_cloud_rules():
    """Absent local legs are INCOMPLETE; an absent cloud leg only fails when requested."""
    expected = expected_legs(platforms=("ubuntu-24.04",), scenarios=("steady",))
    empty = aggregate("does-not-exist", stage="smoke", mode="quick", expected=expected)
    assert empty["verdict"] == "INCOMPLETE"
    assert empty["legs"]["ubuntu-24.04-steady"]["status"] == "missing"
    assert (
        aggregate(
            "does-not-exist", stage="smoke", mode="quick", expected=expected, cloud="not-requested"
        )["cloud"]["status"]
        == "not-requested"
    )
    assert (
        aggregate("does-not-exist", stage="soak", mode="full", expected=expected, cloud="required")[
            "verdict"
        ]
        == "FAIL"
    )


def test_aggregate_and_markdown_name_the_reason(tmp_path):
    """The final summary must state why a stage is not PASS."""
    directory = _report(tmp_path)
    reports = directory.parent
    result = aggregate(
        reports,
        stage="smoke",
        mode="quick",
        expected=["ubuntu-24.04-steady"],
        expected_commit="a" * 40,
    )
    assert result["verdict"] == "PASS"
    markdown = render_markdown(result)
    assert "| ubuntu-24.04-steady | PASS |" in markdown
    broken = aggregate(reports, stage="smoke", mode="quick", expected=["ubuntu-24.04-restart"])
    assert "no report artifact" in render_markdown(broken)
    assert broken["verdict"] == "INCOMPLETE"


def test_expected_legs_covers_both_architectures_and_three_scenarios():
    """The matrix is exactly the six documented runners."""
    legs = expected_legs()
    assert len(legs) == 6
    assert "ubuntu-24.04-steady" in legs
    assert "ubuntu-24.04-arm-network" in legs


def test_read_json_tolerates_a_truncated_document(tmp_path):
    """A crash must not raise from a report reader."""
    from scripts.soak.common import read_json

    path = tmp_path / "broken.json"
    path.write_text('{"status": "PA', encoding="utf-8")
    assert read_json(path) is None
    assert read_json(tmp_path / "absent.json") is None


def test_module_entry_points_exist():
    """Every documented harness module stays importable as a module."""
    import importlib

    for name in (
        "scripts.soak.aggregate",
        "scripts.soak.common",
        "scripts.soak.load",
        "scripts.soak.prepare",
        "scripts.soak.run",
        "scripts.soak.stub",
        "scripts.soak.supervisor",
        "scripts.soak.verify",
    ):
        assert importlib.import_module(name) is not None
    module_file = sys.modules["scripts.soak.run"].__file__
    assert module_file is not None
    assert Path(module_file).name == "run.py"


def test_network_operator_recovery_waits_for_active_fault_and_coalesces_graceful_restart(
    monkeypatch,
):
    """Recovery waits until proxies are healthy and shares one graceful restart."""
    import asyncio
    from types import SimpleNamespace

    from scripts.soak.run import Runner

    runner = Runner.__new__(Runner)
    runner._owner_recovery_generation = 0
    runner._owner_recovery_lock = asyncio.Lock()
    runner._owner_recovery_tasks = {}
    runner._active_network_fault_cleared = asyncio.Event()
    runner._active_network_fault_abort = asyncio.Event()
    runner.start_failures = 0
    runner.fatal = ""
    from pathlib import Path

    runner.env = SimpleNamespace(
        home=Path("/tmp/soak-profile"), cloud=False, ca_file="/tmp/ca.pem", llm_key=""
    )
    restart_calls = []
    journal_rows = []

    def restart(*, force, before_start=None):
        """Record the simulated Owner restart and run its maintenance callback."""
        restart_calls.append(force)
        before_start()
        return {"ready_seconds": 0.25}

    runner.supervisor = SimpleNamespace(restart=restart)
    runner.journal = SimpleNamespace(
        record=lambda kind, **fields: journal_rows.append((kind, fields))
    )

    import scripts.soak.run as run_module

    retry_commands = []
    monkeypatch.setattr(
        run_module.subprocess,
        "run",
        lambda command, **kwargs: (
            retry_commands.append((command, kwargs))
            or SimpleNamespace(
                returncode=0, stdout='{"ledger": {"events": {"PENDING": 1}}}', stderr=""
            )
        ),
    )

    async def run():
        """Exercise recovery waiting for proxy health before restarting."""
        scheduled = await asyncio.gather(
            *(
                runner.request_owner_recovery_after_shared_gate(
                    0, f"hermes-{number}", f"request-{number}"
                )
                for number in range(3)
            )
        )
        await asyncio.sleep(0)
        assert restart_calls == []
        assert scheduled == [True, True, True]
        runner._active_network_fault_cleared.set()
        return await asyncio.gather(*runner._owner_recovery_tasks.values())

    assert asyncio.run(run()) == [True]
    assert restart_calls == [False]
    assert retry_commands[0][0][2:7] == [
        "qdrant_memory.standalone",
        "--home",
        str(runner.env.home),
        "retry",
        "--policy",
    ]
    assert retry_commands[0][0][-1] == "./policy.json"
    assert retry_commands[0][1]["cwd"] == Path("/tmp/soak-profile")
    assert [fields["event"] for kind, fields in journal_rows][-4:] == [
        "operator_recovery_started",
        "operator_retry_started",
        "operator_retry_completed",
        "operator_recovery_completed",
    ]
    assert runner._owner_recovery_generation == 1
    assert [fields["event"] for kind, fields in journal_rows] == [
        "operator_recovery_requested",
        "operator_recovery_coalesced",
        "operator_recovery_coalesced",
        "operator_recovery_started",
        "operator_retry_started",
        "operator_retry_completed",
        "operator_recovery_completed",
    ]


def test_probe_task_retries_deferred_authorization_with_the_same_marker(monkeypatch):
    """A temporary gate leaves the auth probe queued and postpones other probes."""
    import asyncio
    from types import SimpleNamespace

    import scripts.soak.run as run_module

    runner = run_module.Runner.__new__(run_module.Runner)
    runner.args = SimpleNamespace(target_seconds=60, scenario="network", profile="quick")
    runner.stop_at = 1.0
    runner.fatal = ""
    runner.client = object()
    runner.journal = object()
    runner.env = SimpleNamespace(tokens={}, owner_endpoint="", ca_file="", untrusted_ca="")
    markers = []
    calls = {"cas": 0, "retry": 0}
    ticks = iter((0.0, 0.0, 2.0))

    async def no_sleep(_delay):
        """Skip probe cadence sleeps in this focused unit test."""
        return None

    async def authorization(_client, _journal, *, marker=None):
        """Return one deferred authorization probe followed by a pass."""
        markers.append(marker)
        return (
            {"status": "DEFERRED", "marker": "same-marker"}
            if len(markers) == 1
            else {"status": "PASS"}
        )

    async def cas(_client, _journal, *, marker=None):
        """Record that the CAS probe was allowed to run."""
        calls["cas"] += 1
        return {"status": "PASS", "marker": marker}

    async def retry_contract(*_args, **_kwargs):
        """Record that the retry-contract probe was allowed to run."""
        calls["retry"] += 1

    monkeypatch.setattr(run_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(run_module, "probe_authorization", authorization)
    monkeypatch.setattr(run_module, "probe_cas", cas)
    monkeypatch.setattr(run_module, "probe_retry_contract", retry_contract)

    asyncio.run(runner.probe_task(clock=lambda: next(ticks)))

    assert markers == [None, "same-marker"]
    assert calls == {"cas": 1, "retry": 1}


def test_probe_task_waits_for_probe_retry_after(monkeypatch):
    """A deferred CAS probe does not violate the Owner's Retry-After."""
    import asyncio
    from types import SimpleNamespace

    import scripts.soak.run as run_module

    runner = run_module.Runner.__new__(run_module.Runner)
    runner.args = SimpleNamespace(target_seconds=60, scenario="network", profile="quick")
    runner.stop_at = 110.0
    runner.fatal = ""
    runner.client = object()
    runner.journal = object()
    runner.env = SimpleNamespace(tokens={}, owner_endpoint="", ca_file="", untrusted_ca="")
    now = [0.0]
    cas_calls = []

    async def advance(delay):
        """Advance the fake clock without waiting in real time."""
        now[0] += delay

    async def authorization(_client, _journal, *, marker=None):
        """Return a successful authorization probe."""
        return {"status": "PASS", "marker": marker}

    async def cas(_client, _journal, *, marker=None):
        """Defer once, then complete the CAS probe after Retry-After."""
        cas_calls.append((marker, now[0]))
        if len(cas_calls) == 1:
            return {"status": "DEFERRED", "marker": "same-cas", "retry_after": 90}
        return {"status": "PASS", "marker": marker}

    async def retry_contract(*_args, **_kwargs):
        """Stub the unrelated retry-contract probe."""
        return None

    monkeypatch.setattr(run_module.asyncio, "sleep", advance)
    monkeypatch.setattr(run_module, "probe_authorization", authorization)
    monkeypatch.setattr(run_module, "probe_cas", cas)
    monkeypatch.setattr(run_module, "probe_retry_contract", retry_contract)

    asyncio.run(runner.probe_task(clock=lambda: now[0]))

    assert cas_calls == [(None, 10.0), ("same-cas", 100.0)]


def test_cancelling_fault_cycle_does_not_cancel_operator_recovery():
    """Window shutdown must leave the separate operator restart alive to join."""
    import asyncio
    import time
    from types import SimpleNamespace

    from scripts.soak.run import Runner

    runner = Runner.__new__(Runner)
    runner.args = SimpleNamespace(target_seconds=1000)
    runner.stop_at = time.monotonic() + 1000
    runner.fatal = ""
    runner._owner_recovery_tasks = {}
    runner._active_network_fault_cleared = asyncio.Event()
    runner._active_network_fault_cleared.set()
    runner._active_network_fault_abort = asyncio.Event()
    recovery_started = asyncio.Event()
    finish_recovery = asyncio.Event()

    async def recover():
        """Hold recovery open until the test allows it to finish."""
        recovery_started.set()
        await finish_recovery.wait()

    async def run():
        """Cancel the network driver while recovery is still pending."""
        recovery = asyncio.create_task(recover())
        runner._owner_recovery_tasks[0] = recovery
        plan = (("client", "latency", {}, 1.0),)
        fault_cycle = asyncio.create_task(runner._fault_cycle(None, plan, 1, time.monotonic))
        await recovery_started.wait()
        await asyncio.sleep(0)
        fault_cycle.cancel()
        with pytest.raises(asyncio.CancelledError):
            await fault_cycle
        assert not recovery.cancelled()
        finish_recovery.set()
        await recovery

    asyncio.run(run())


def test_failed_proxy_cleanup_aborts_recovery_without_signalling_healthy():
    """An unconfirmed proxy state cannot release the waiting Owner restart."""
    import asyncio
    from types import SimpleNamespace

    from scripts.soak.run import Runner

    runner = Runner.__new__(Runner)
    runner._active_network_fault_cleared = asyncio.Event()
    runner._active_network_fault_abort = asyncio.Event()
    runner.fatal = ""
    journal_rows = []
    runner.journal = SimpleNamespace(
        record=lambda kind, **fields: journal_rows.append((kind, fields))
    )

    async def cleanup_failed(_toxic):
        """Simulate a proxy cleanup operation that cannot be confirmed."""
        return [{"proxy": "qdrant", "operation": "clear_toxics", "error": {"type": "OSError"}}]

    runner._clear_network_faults = cleanup_failed

    async def run():
        """Check that failed cleanup leaves recovery blocked and aborted."""
        assert not await runner._complete_fault_cleanup(None, "client_disconnect")
        assert not runner._active_network_fault_cleared.is_set()
        assert runner._active_network_fault_abort.is_set()
        return await runner._wait_for_network_fault_resolution()

    assert asyncio.run(run()) is False
    assert runner.fatal == "network_fault_cleanup_failed"
    assert journal_rows[0][1]["event"] == "fault_cleanup_failed"


def test_aborted_proxy_recovery_does_not_restart_owner():
    """A cleanup failure releases recovery as failed, never as a healthy restart."""
    import asyncio
    from types import SimpleNamespace

    from scripts.soak.run import Runner

    runner = Runner.__new__(Runner)
    runner._active_network_fault_cleared = asyncio.Event()
    runner._active_network_fault_abort = asyncio.Event()
    runner._owner_recovery_lock = asyncio.Lock()
    runner._owner_recovery_generation = 0
    runner.fatal = "network_fault_cleanup_failed"
    restart_calls = []
    journal_rows = []
    runner.supervisor = SimpleNamespace(restart=lambda **kwargs: restart_calls.append(kwargs))
    runner.journal = SimpleNamespace(
        record=lambda kind, **fields: journal_rows.append((kind, fields))
    )

    async def run():
        """Verify cleanup failure aborts recovery before an Owner restart."""
        recovery = asyncio.create_task(
            runner._restart_owner_after_shared_gate(0, "hermes-a", "id-1")
        )
        await asyncio.sleep(0)
        runner._active_network_fault_abort.set()
        return await recovery

    assert asyncio.run(run()) is False
    assert restart_calls == []
    assert journal_rows[0][1]["event"] == "operator_recovery_aborted"


def test_network_rate_limit_probe_uses_post_window_deadline(monkeypatch):
    """A slow operator restart cannot defer the independent rate-limit probe."""
    import asyncio
    from types import SimpleNamespace

    import scripts.soak.run as run_module
    from scripts.soak.run import Runner

    runner = Runner.__new__(Runner)
    runner.args = SimpleNamespace(scenario="network", target_seconds=0, workers=1)
    runner.client = SimpleNamespace(deadline=None)
    runner.workload = SimpleNamespace(run=lambda **kwargs: None)
    runner.stop_at = None
    runner.fatal = ""
    runner._owner_recovery_tasks = {}
    runner.journal = SimpleNamespace(record=lambda *_args, **_kwargs: None)

    async def no_op_task():
        """Wait until the Runner cancels its background task."""
        await asyncio.Event().wait()

    runner.metrics_task = no_op_task
    runner.probe_task = no_op_task
    runner.network_task = no_op_task
    runner.lost_response_task = no_op_task
    observed_deadlines = []

    async def rate_limit_probe(client, _journal):
        """Capture the deadline visible to the post-window probe."""
        observed_deadlines.append(client.deadline)

    async def workload_run(**_kwargs):
        """Finish the fake workload without issuing network calls."""
        return None

    runner.workload.run = workload_run
    monkeypatch.setattr(run_module, "probe_rate_limit", rate_limit_probe)

    asyncio.run(runner.run())

    assert observed_deadlines == [None]
