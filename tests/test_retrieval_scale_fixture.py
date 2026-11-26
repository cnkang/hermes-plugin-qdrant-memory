"""Keep the larger retrieval fixture synthetic, hard and honestly labeled."""

import json
import re
import runpy
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "retrieval-scale-v1.json"


def evaluator():
    """Load the existing retrieval evaluator so this fixture reuses its validator."""
    return runpy.run_path(str(ROOT / "scripts" / "evaluate.py"))


@pytest.fixture(scope="module")
def fixture():
    """Return the parsed fixture only after the evaluator accepts every label."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    evaluator()["validate_dataset"](data)
    return data


def test_fixture_scale_and_slices(fixture):
    """The fixture must be large enough to stress ranking, not just smoke test it."""
    assert 300 <= len(fixture["memories"]) <= 1000
    assert 60 <= len(fixture["queries"]) <= 120
    counts = Counter(query["category"] for query in fixture["queries"])
    for language in ("en", "zh", "mixed"):
        assert counts[language] >= 18, language
    assert counts["multi-relevant"] >= 1
    assert counts["abstention"] >= 4
    assert fixture["allow_abstention"] is True


def test_fixture_contains_near_duplicate_entities_and_shared_values(fixture):
    """Hard negatives need near-identical names and repeated attribute values."""
    prefixes = {memory["id"].split("-")[0] for memory in fixture["memories"]}
    assert {"cedar", "cedra", "rowan"} <= prefixes
    texts = "\n".join(memory["text"] for memory in fixture["memories"])
    for shared in ("SQLite WAL", "a local file lease", "PostgreSQL"):
        assert texts.count(shared) >= 4, shared


def test_queries_never_embed_the_gold_memory_text(fixture):
    """A query must describe an information need, not quote the stored record."""
    memories = [memory["text"].strip().casefold() for memory in fixture["memories"]]
    for query in fixture["queries"]:
        lowered = query["query"].strip().casefold()
        assert lowered not in memories, query["query"]
        assert not any(memory in lowered for memory in memories), query["query"]


def test_abstention_queries_carry_no_labels(fixture):
    """An abstention query must have no relevant memory and stay out of recall scoring."""
    abstentions = [query for query in fixture["queries"] if not query["relevant_ids"]]
    assert len(abstentions) >= 4
    assert all(query["category"] == "abstention" for query in abstentions)


def test_fixture_stays_synthetic_and_portable(fixture):
    """No real personal data, local path or credential may enter a public fixture."""
    text = FIXTURE.read_text(encoding="utf-8")
    assert "/Users/" not in text and "/Volumes/" not in text
    assert not re.search(r"sk-[A-Za-z0-9]{16}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20}", text)
    assert all(memory["text"].strip() for memory in fixture["memories"])


def test_abstention_is_rejected_without_the_opt_in():
    """An empty label stays invalid unless the dataset opts into abstention scoring."""
    validate = evaluator()["validate_dataset"]
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    without_opt_in = {key: value for key, value in data.items() if key != "allow_abstention"}
    with pytest.raises(ValueError):
        validate(without_opt_in)


def test_query_variants_name_the_subject_without_leaking_answers():
    """A focused query must name the project, and neither variant may quote the gold record."""
    variants_path = ROOT / "tests/fixtures" / "query-construction-v1.json"
    corpus = json.loads(FIXTURE.read_text(encoding="utf-8"))
    variants = json.loads(variants_path.read_text(encoding="utf-8"))
    runner = runpy.run_path(str(ROOT / "scripts" / "evaluate_query_variants.py"))
    runner["validate"](corpus, variants)
    by_id = {memory["id"]: memory["text"] for memory in corpus["memories"]}
    assert len(variants["queries"]) >= 10
    for query in variants["queries"]:
        gold = by_id[query["relevant_ids"][0]]
        project = re.search(r"Project ([A-Za-z0-9-]+)", gold).group(1)
        assert query["broad"] != query["focused"]
        assert project not in query["broad"], query["id"]
        assert project in query["focused"], query["id"]
        for variant in ("broad", "focused"):
            assert gold.casefold() not in query[variant].casefold(), query["id"]
