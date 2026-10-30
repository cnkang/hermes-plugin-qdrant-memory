"""Evidence, statistics and redaction contracts of the soak harness."""

import json
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

from scripts.soak.aggregate import aggregate, expected_legs, render_markdown, validate_report
from scripts.soak.common import (
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
    assert "https://llm.example" in public


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
    passed = [Invariant(name=f"check_{index}", status="PASS") for index in range(9)]
    status, _ = overall_status(passed, duration_seconds=480, target_seconds=480, harness_error=None)
    assert status == "PASS"
    failing = [*passed[:8], Invariant(name="check_last", status="FAIL")]
    assert overall_status(failing, duration_seconds=480, target_seconds=480)[0] == "FAIL"
    assert overall_status(passed, duration_seconds=200, target_seconds=480)[0] == "INCOMPLETE"
    assert (
        overall_status(passed, duration_seconds=480, target_seconds=480, harness_error="boom")[0]
        == "HARNESS_ERROR"
    )
    duplicate = [Invariant(name="same", status="PASS") for _ in range(9)]
    assert overall_status(duplicate, duration_seconds=480, target_seconds=480)[0] == "INCOMPLETE"


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
        runtime_version="0.5.2",
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
        "invariants": [{"name": "write_durability", "status": "PASS"}],
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
    assert module_file is not None and Path(module_file).name == "run.py"
