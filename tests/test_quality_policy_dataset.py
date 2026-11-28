"""Keep the admission dataset labeled, complete and honestly scored before any model run."""

import json
import re
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from qdrant_memory.quality_metrics import latency_summary

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "tests" / "quality" / "memory-policy-v1.json"
GROUPS = ("A", "B", "C", "D", "E", "F", "G", "H")
SECRET_SHAPES = re.compile(
    r"sk-[A-Za-z0-9]{16}|AKIA[0-9A-Z]{16}|xoxb-[A-Za-z0-9-]{10}|ghp_[A-Za-z0-9]{20}"
)


def runner():
    """Load this checkout's evaluator independently of ambient scripts packages."""
    return runpy.run_path(str(ROOT / "scripts" / "evaluate_lifecycle.py"))


@pytest.fixture(scope="module")
def dataset():
    """Return the validated dataset produced by the runner's own loader, not a copy."""
    return runner()["load_policy_dataset"](DATASET)


def save_case(**overrides):
    """Build one minimal labeled save case."""
    case = {"id": "s", "group": "A", "expect": "save", "user": "", "terms": [["Cedar"]], "note": ""}
    return {**case, **overrides}


def skip_case(**overrides):
    """Build one minimal labeled skip case."""
    case = {"id": "n", "group": "A", "expect": "skip", "user": "", "terms": [], "note": ""}
    return {**case, **overrides}


def test_dataset_covers_every_required_group(dataset):
    """Each documented scenario class must have both admitted and rejected examples."""
    seen = {case["group"] for case in dataset["cases"]}
    assert seen == set(GROUPS)
    for group in GROUPS:
        cases = [case for case in dataset["cases"] if case["group"] == group]
        assert len(cases) >= 4, group
        assert {"save", "skip"} <= {case["expect"] for case in cases}, group


def test_terms_are_word_complete(dataset):
    """A Latin term must not be a prefix of a longer word, or the shared matcher misses it."""
    for case in dataset["cases"]:
        for group in case["terms"]:
            for term in group:
                if re.fullmatch(r"[A-Za-z0-9_.:-]+", term):
                    assert not re.search(
                        rf"(?<![A-Za-z0-9_]){re.escape(term)}[a-z]", case["user"]
                    ), (case["id"], term)


def test_dataset_ids_and_notes_are_auditable(dataset):
    """Every label carries a reason, and identifiers stay unique and stable."""
    ids = [case["id"] for case in dataset["cases"]]
    assert len(ids) == len(set(ids))
    assert all(case["note"].strip() for case in dataset["cases"])


def test_dataset_includes_multilingual_and_injection_cases(dataset):
    """Chinese, mixed-language and untrusted-instruction cases must stay present."""
    cjk = [case for case in dataset["cases"] if re.search(r"[\u4e00-\u9fff]", case["user"])]
    mixed = [case for case in cjk if re.search(r"[A-Za-z]{3}", case["user"])]
    trusted = [
        case for case in dataset["cases"] if case["group"] == "G" and case["expect"] == "skip"
    ]
    assert len(cjk) >= 3
    assert mixed
    assert len(trusted) >= 4


def test_dataset_holds_no_real_credentials_or_local_paths():
    """Synthetic fixtures must stay obviously fake and portable."""
    text = DATASET.read_text(encoding="utf-8")
    assert "TEST_ONLY" in text and "SYNTHETIC-ONLY" in text
    assert not SECRET_SHAPES.search(text)
    assert "/Users/" not in text and "/Volumes/" not in text


def test_rejected_malformed_labels_never_reach_a_model():
    """A wrong or missing label must fail closed instead of scoring itself."""
    validate = runner()["validate_policy_dataset"]
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    with pytest.raises(ValueError):
        validate(
            {**data, "groups": {key: value for key, value in data["groups"].items() if key != "H"}}
        )
    with pytest.raises(ValueError):
        validate({**data, "cases": [{**data["cases"][0], "expect": "maybe"}]})
    broken = dict(data["cases"][0], expect="skip", terms=[["Cedar"]])
    with pytest.raises(ValueError):
        validate({**data, "cases": [broken]})
    with pytest.raises(ValueError):
        validate({**data, "cases": [{**save_case(), "require_patterns": ["("]}]})


def test_scoring_rejects_duplicates_and_blank_output():
    """A duplicate record is redundant, and a blank string is not a saved memory."""
    score, verdict, rates = runner()["score_cases"], runner()["case_verdict"], runner()["rates"]
    duplicate = verdict(save_case(), ["Cedar uses SQLite.", "Cedar uses SQLite."])
    assert duplicate["outcome"] == "duplicate"
    assert duplicate["true_rows"] == 1 and duplicate["false_rows"] == 1

    blank = verdict(save_case(), ["   "])
    assert blank["outcome"] == "invalid_output"
    assert blank["invalid_rows"] == 1 and blank["true_rows"] == 0
    assert verdict(skip_case(), ["   "])["outcome"] == "invalid_output"
    skipped, _ = score([skip_case()], {"n": ["   "]})
    assert skipped["skip_clean"] == 0
    assert rates(skipped)["false_memory_rate"] is None

    total, _ = score([save_case()], {"s": ["Cedar uses SQLite."]})
    cleaned = rates(total)
    assert cleaned["admission_precision"] == 1.0 and cleaned["duplicate_rows"] == 0


def test_scoring_requires_negation_and_condition_wording():
    """A keyword hit alone must not pass a negated or conditional record."""
    verdict = runner()["case_verdict"]
    negated = save_case(terms=[["telemetry"]], require_patterns=[r"\b(do not|must not|never)\b"])
    missed = verdict(negated, ["Telemetry is enabled for this project."])
    assert missed["outcome"] == "missed" and missed["true_rows"] == 0
    passed = verdict(negated, ["Do not send memory content to external telemetry."])
    assert passed["outcome"] == "correct" and passed["true_rows"] == 1

    conditional = save_case(
        terms=[["writer"], ["isolated"]], require_patterns=[r"\b(unless|only if|when)\b"]
    )
    assert verdict(conditional, ["The writer rule and isolated mode."])["outcome"] == "missed"
    assert (
        verdict(conditional, ["The single-writer rule applies only if every writer is isolated."])[
            "outcome"
        ]
        == "correct"
    )


def test_forbidden_wording_is_never_scored_as_correct():
    """A record with the wrong entity or an inverted rule must fail even with the right keywords."""
    verdict = runner()["case_verdict"]
    case = save_case(terms=[["Cedar"]], forbid_patterns=[r"\bRowan\b"])
    result = verdict(case, ["Rowan uses SQLite."])
    assert result["outcome"] == "false_memory" or result["outcome"] == "forbidden_wording"
    assert result["true_rows"] == 0


def test_provider_failure_is_not_a_correct_rejection():
    """A failed model call must leave the denominator, not count as a clean skip."""
    score, rates = runner()["score_cases"], runner()["rates"]
    total, _ = score([save_case(), skip_case()], {}, failures={"s", "n"})
    assert total["failed_cases"] == 2 and total["cases_scored"] == 0
    empty = rates(total)
    assert empty["admission_precision"] is None
    assert empty["admission_recall"] is None
    assert empty["false_memory_rate"] is None
    assert total["skip_clean"] == 0


@pytest.mark.parametrize(
    "problem", ["empty", "missing", "extra", "duplicate", "missing_rows", "lost_invalid_rows"]
)
def test_rescore_rejects_incomplete_or_ambiguous_reports(tmp_path, dataset, problem):
    """A missing case result cannot supply an empty extraction or a clean rejection."""
    stored = [{"id": case["id"], "rows": []} for case in dataset["cases"]]
    if problem == "empty":
        stored.clear()
    elif problem == "missing":
        stored.pop()
    elif problem == "extra":
        stored.append({"id": "unexpected", "rows": []})
    elif problem == "duplicate":
        stored.append(stored[0])
    elif problem == "missing_rows":
        del stored[0]["rows"]
    else:
        stored[0]["invalid_rows"] = 1
    source, output = tmp_path / "source.json", tmp_path / "output.json"
    source.write_text(json.dumps({"case_results": stored}), encoding="utf-8")
    with pytest.raises(ValueError, match=r"(?i)stored (result|case IDs)"):
        runner()["rescore"](DATASET, source, output)
    assert not output.exists()


def test_rescore_keeps_explicit_empty_results_and_provider_failures(tmp_path, dataset):
    """Only recorded results contribute to coverage, and failed calls stay unscored."""
    failed, invalid, skipped = [case for case in dataset["cases"] if case["expect"] == "skip"][:3]
    stored = [
        {"id": case["id"], "rows": [" "] if case == invalid else []} for case in dataset["cases"]
    ]
    next(row for row in stored if row["id"] == failed["id"])["outcome"] = "provider_failed"
    source, output = tmp_path / "source.json", tmp_path / "output.json"
    source.write_text(json.dumps({"case_results": stored}), encoding="utf-8")
    assert runner()["rescore"](DATASET, source, output) == 1
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["execution"] == {
        "status": "FAIL",
        "provider_failures": 1,
        "invalid_cases": 1,
        "cases_with_result": len(stored) - 2,
        "complete": False,
    }
    by_id = {row["id"]: row for row in result["case_results"]}
    assert by_id[skipped["id"]]["outcome"] == "correct"
    assert by_id[invalid["id"]]["invalid_rows"] == 1
    assert by_id[failed["id"]]["outcome"] == "provider_failed"
    assert result["false_memory_rate"] is None


def test_mixed_invalid_or_failed_output_suppresses_otherwise_perfect_rates():
    """A missing or invalid response cannot improve the reported run quality."""
    score, rates = runner()["score_cases"], runner()["rates"]
    cases = [save_case(), skip_case()]
    for rows, failures in [
        ({"s": ["Cedar uses SQLite."]}, {"n"}),
        ({"s": ["Cedar uses SQLite.", None], "n": []}, ()),
    ]:
        total, _ = score(cases, rows, failures)
        assert rates(total)["admission_precision"] is None
        assert rates(total)["admission_recall"] is None
        assert rates(total)["false_memory_rate"] is None
    with pytest.raises(ValueError, match="one result"):
        score(cases, {"s": ["Cedar uses SQLite."]})


def test_rates_report_denominators_and_clean_scoring():
    """Rates come from labeled rows and disclose their denominators."""
    score, rates = runner()["score_cases"], runner()["rates"]
    cases = [save_case(), skip_case()]
    clean, _ = score(cases, {"s": ["Project Cedar uses SQLite."], "n": []})
    summary = rates(clean)
    assert summary["admission_precision"] == 1.0
    assert summary["admission_recall"] == 1.0
    assert summary["false_memory_rate"] == 0.0
    assert summary["rows_denominator"] == 1 and summary["save_denominator"] == 1
    assert summary["skip_denominator"] == 1

    noisy, _ = score(cases, {"s": ["unrelated text"], "n": ["temporary status"]})
    assert rates(noisy)["admission_precision"] == 0.0
    assert rates(noisy)["admission_recall"] == 0.0
    assert rates(noisy)["false_memory_rate"] == 1.0


def test_execution_status_separates_health_from_quality():
    """Dataset runs report completion, while the inline diagnostic keeps its strict gate."""
    status = runner()["execution_status"]
    assert status("inline-diagnostic", 0, 1.0) == "PASS"
    assert status("inline-diagnostic", 0, 0.9) == "FAIL"
    assert status("inline-diagnostic", 1, 1.0) == "FAIL"
    assert status("policy-admission", 0, 0.5) == "COMPLETE"
    assert status("policy-admission", 1, 1.0) == "FAIL"


def test_percentiles_use_the_shared_nearest_rank_helper():
    """p95 must be the nearest-rank percentile, not the maximum sample."""
    assert runner()["latency_summary"] is latency_summary
    samples = [float(value) for value in range(1, 101)]
    assert latency_summary(samples) == {"samples": 100, "p50_ms": 50.0, "p95_ms": 95.0}
    assert max(samples) == 100.0
    assert latency_summary([]) == {"samples": 0, "p50_ms": None, "p95_ms": None}


def test_shared_benchmark_timings_use_the_same_percentiles():
    """The shared benchmark must not report the maximum sample as p95 either."""
    timings = runpy.run_path(str(ROOT / "scripts" / "evaluate_shared.py"))["timings"]
    assert timings([float(value) for value in range(1, 21)]) == {
        "samples": 20,
        "p50_ms": 10.0,
        "p95_ms": 19.0,
        "max_ms": 20.0,
    }
    assert timings([]) == {"samples": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}


def test_runner_exposes_the_dataset_option():
    """Operators must be able to select the labeled dataset without editing the script."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "evaluate_lifecycle.py"), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--dataset" in result.stdout
    assert "--model" in result.stdout
