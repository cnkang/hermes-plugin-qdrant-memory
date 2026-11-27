"""Check that the behavior A/B report retains the evidence needed for re-scoring."""

import json
import runpy
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def runner():
    """Load the checkout's A/B script without running Hermes."""
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "behavior_ab.py"))


@pytest.mark.parametrize(
    ("tool", "target_id", "status"),
    [
        ("qdrant_memory_update", "expected", "pass"),
        ("mcp__memory__qdrant_memory_update", "expected", "pass"),
        ("qdrant_memory_update", "wrong", "fail"),
        ("qdrant_memory_delete", "expected", "fail"),
        ("qdrant_memory_delete", "wrong", "fail"),
    ],
)
def test_exact_update_matches_live_and_rescored_result(runner, tool, target_id, status):
    """The exact ID only counts on an update, and that evidence survives JSON."""
    scenario = {"id": "edit", "expect_write": True, "expect_update_id": "expected"}
    parsed = {
        "tools": [{"name": tool, "input": {"id": target_id}}],
        "text": "Done.",
        "result": {"exit_code": 0},
    }
    live = runner["summarize_run"](parsed, scenario, {}, process_exit_code=0)
    saved = json.loads(json.dumps({"scenarios": [live]}))
    assert live["status"] == status
    assert runner["rescore_report"](saved, [scenario])["scenarios"] == [live]


@pytest.mark.parametrize(
    ("process", "terminal", "text", "status"),
    [
        (1, 0, "Done.", "error"),
        (0, 1, "Done.", "error"),
        (0, None, "Done.", "incomplete"),
        (0, 0, "", "incomplete"),
        (0, 0, "Done.", "pass"),
    ],
)
def test_run_completion_requires_both_success_signals(runner, process, terminal, text, status):
    """A missing or failed completion cannot pass a no-write scenario."""
    scenario = {"id": "no-write", "expect_write": False}
    parsed = {"tools": [], "text": text, "result": {"exit_code": terminal}}
    live = runner["summarize_run"](parsed, scenario, {}, process_exit_code=process)
    assert live["status"] == status
    assert runner["rescore_report"]({"scenarios": [live]}, [scenario])["scenarios"] == [live]


def test_rescore_checks_coverage_and_marks_legacy_evidence_incomplete(runner):
    """Missing scenarios fail coverage, and truncated legacy traces cannot prove success."""
    scenario = {"id": "example"}
    row = {"id": "example", "text": "Done.", "status": "pass"}
    for rows in ([], [row, row], [{**row, "id": "unexpected"}]):
        with pytest.raises(ValueError, match="scenario IDs"):
            runner["rescore_report"]({"scenarios": rows}, [scenario])
    assert (
        runner["rescore_report"]({"scenarios": [row]}, [scenario])["scenarios"][0]["status"]
        == "incomplete"
    )


def test_rescore_preserves_text_after_old_preview_limit(runner):
    """Match against the full saved reply, including content after the old preview."""
    scenario = {"id": "long", "must_not_contain": ["forbidden"]}
    parsed = {"tools": [], "text": "x" * 1200 + "forbidden", "result": {"exit_code": 0}}
    live = runner["summarize_run"](parsed, scenario, {}, process_exit_code=0)
    assert live["status"] == "fail"
    assert runner["rescore_report"]({"scenarios": [live]}, [scenario])["scenarios"] == [live]


def test_stream_uses_the_final_answer_when_chunks_are_missing(runner):
    """The terminal result can supply a complete reply even without text events."""
    parsed = runner["parse_stream"](
        json.dumps({"type": "result", "exit_code": 0, "text": "Final answer"})
    )
    scenario = {"id": "reply", "must_contain": ["Final answer"]}
    assert runner["summarize_run"](parsed, scenario, {}, process_exit_code=0)["status"] == "pass"


def test_rescore_cli_needs_no_live_profile(runner, tmp_path):
    """Saved evidence can be checked without configuring or mutating a Hermes profile."""
    scenario_path, source, output = (tmp_path / name for name in ("cases", "report", "output"))
    scenario_path.write_text(json.dumps([{"id": "example"}]), encoding="utf-8")
    source.write_text(
        json.dumps({"scenarios": [{"id": "example", "text": "Done.", "status": "pass"}]}),
        encoding="utf-8",
    )
    assert (
        runner["main"](
            [
                "--rescore-report",
                str(source),
                "--scenarios",
                str(scenario_path),
                "--rescore-out",
                str(output),
            ]
        )
        == 0
    )
    assert json.loads(output.read_text(encoding="utf-8"))["scenarios"][0]["status"] == "incomplete"
