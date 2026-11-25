"""Keep the multilingual retrieval fixture labeled, cross-lingual and portable."""

import json
import re
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "retrieval-multilingual.json"
IDENTIFIERS = (
    "WriterLease",
    "qdrant-memory upgrade plan",
    "test_upgrade_watermark",
    "qwen3-embedding:4b",
)


def evaluator():
    """Load the existing retrieval evaluator so this fixture reuses its validator."""
    return runpy.run_path(str(ROOT / "scripts" / "evaluate.py"))


@pytest.fixture(scope="module")
def fixture():
    """Return the parsed fixture only after the evaluator accepts every label."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    evaluator()["validate_dataset"](data)
    return data


def test_every_memory_has_an_english_and_a_chinese_query(fixture):
    """Each fact must be reachable from both languages without a translated copy."""
    for language in ("en", "zh"):
        covered = {
            identifier
            for query in fixture["queries"]
            if query["category"] == language
            for identifier in query["relevant_ids"]
        }
        assert covered == {memory["id"] for memory in fixture["memories"]}, language


def test_mixed_language_queries_stay_present(fixture):
    """Mixed Chinese and English queries must remain a first-class slice."""
    categories = {query["category"] for query in fixture["queries"]}
    assert categories == {"en", "zh", "mixed"}
    assert sum(query["category"] == "mixed" for query in fixture["queries"]) >= 4


def test_queries_keep_exact_identifiers(fixture):
    """Technical identifiers must survive in queries instead of being translated."""
    text = "\n".join(query["query"] for query in fixture["queries"])
    assert sum(identifier in text for identifier in IDENTIFIERS) >= 3


def test_fixture_stays_synthetic_and_portable(fixture):
    """No real personal data, local path or credential may enter a public fixture."""
    text = FIXTURE.read_text(encoding="utf-8")
    assert "/Users/" not in text and "/Volumes/" not in text
    assert not re.search(r"sk-[A-Za-z0-9]{16}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20}", text)
    assert all(memory["text"].strip() for memory in fixture["memories"])
