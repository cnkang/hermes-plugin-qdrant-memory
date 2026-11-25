"""Keep the admission dataset labeled, complete and honestly scored before any model run."""

import json
import re
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_rejected_malformed_labels_never_reach_a_model(tmp_path):
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


def test_scoring_counts_false_memories_and_empty_runs():
    """Rates must come from labeled rows, and an empty run must not divide by zero."""
    score = runner()["score_cases"]
    rates = runner()["rates"]
    cases = [
        {"id": "s", "group": "A", "expect": "save", "user": "", "terms": [["Cedar"]], "note": ""},
        {"id": "n", "group": "A", "expect": "skip", "user": "", "terms": [], "note": ""},
    ]
    clean, _ = score(cases, {"s": ["Project Cedar uses SQLite."], "n": []})
    assert rates(clean)["admission_precision"] == 1.0
    assert rates(clean)["admission_recall"] == 1.0
    assert rates(clean)["false_memory_rate"] == 0.0

    noisy, _ = score(cases, {"s": ["unrelated text"], "n": ["temporary status"]})
    assert rates(noisy)["admission_precision"] == 0.0
    assert rates(noisy)["admission_recall"] == 0.0
    assert rates(noisy)["false_memory_rate"] == 1.0
    assert rates(score(cases, {})[0])["admission_precision"] == 0.0


def test_inline_mode_keeps_its_strict_gate():
    """The legacy inline diagnostic must still fail a single missed case."""
    status = runner()["report_status"]
    assert status("inline-diagnostic", 0, 1.0) == "PASS"
    assert status("inline-diagnostic", 0, 0.9) == "FAIL"
    assert status("policy-admission", 0, 0.9) == "PASS"
    assert status("policy-admission", 1, 1.0) == "FAIL"
    assert status("inline-diagnostic", 1, 1.0) == "FAIL"


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
