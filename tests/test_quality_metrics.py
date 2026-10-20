"""The model-quality gate must fail closed on incomplete or misleading evidence."""

import pytest

from qdrant_memory.quality_metrics import score_run


def test_restart_comparison_preserves_payload_but_excludes_computed_scores():
    """Computed float scores may change; stored identity, version and text may not."""
    from qdrant_memory.quality_metrics import durable_snapshot

    before = [{"id": "one", "text": "SQLite WAL", "version": 1, "score": 0.9999999}]
    after = [{**before[0], "score": 1.0}]
    assert durable_snapshot(before) == durable_snapshot(after)
    for field, value in (("id", "two"), ("text", "changed"), ("version", 2)):
        assert durable_snapshot(before) != durable_snapshot([{**after[0], field: value}])
    with pytest.raises(ValueError):
        durable_snapshot(before + before)


def dataset():
    """Provide a positive fact, a negative turn and one gold retrieval."""
    return {
        "cases": [
            {
                "id": "fact",
                "expected": [{"id": "wal", "terms": [["SQLite"], ["WAL", "write-ahead"]]}],
            },
            {"id": "transient", "expected": []},
        ],
        "queries": [{"id": "query", "expected": "wal"}],
    }


def evidence(**changes):
    """Provide complete successful evidence with targeted mutations."""
    return {
        "extractions": {"fact": ["SQLite uses write-ahead logging"], "transient": []},
        "retrievals": {"query": ["wal"]},
        "scope_leaks": 0,
        "provider_failures": 0,
        "replay_safe": True,
        "restart_safe": True,
        **changes,
    }


def test_complete_quality_evidence_passes():
    """A complete correct run meets all independently named metrics."""
    result = score_run(dataset(), evidence())
    assert result["status"] == "PASS"
    assert result["precision"] == result["positive_case_recall"] == 1
    assert result["recall_at_5"] == result["mrr"] == 1


@pytest.mark.parametrize(
    "missing",
    [
        "extractions",
        "retrievals",
        "scope_leaks",
        "provider_failures",
        "replay_safe",
        "restart_safe",
    ],
)
def test_missing_evidence_never_defaults_to_success(missing):
    """Absence of a required measurement cannot become a green gate."""
    observed = evidence()
    del observed[missing]
    assert score_run(dataset(), observed)["status"] == "FAIL"


def test_duplicate_and_negative_capture_are_false_positives():
    """Over-capture lowers precision even when positive recall is perfect."""
    observed = evidence(
        extractions={"fact": ["SQLite WAL", "SQLite WAL"], "transient": ["build running"]}
    )
    result = score_run(dataset(), observed)
    assert result["precision"] == pytest.approx(1 / 3)
    assert result["positive_case_recall"] == 1
    assert result["status"] == "FAIL"


def test_retrieval_rank_counts_once_and_respects_top_five():
    """Duplicates do not inflate recall and ranks beyond five do not qualify."""
    result = score_run(dataset(), evidence(retrievals={"query": [None, "wal", "wal"]}))
    assert result["recall_at_5"] == 1
    assert result["mrr"] == 0.5
    assert result["status"] == "FAIL"
    result = score_run(dataset(), evidence(retrievals={"query": [None] * 5 + ["wal"]}))
    assert result["recall_at_5"] == 0
    assert result["status"] == "FAIL"


@pytest.mark.parametrize(
    "extra",
    [{"scope_leaks": 1}, {"provider_failures": 1}, {"replay_safe": False}, {"restart_safe": False}],
)
def test_integrity_or_provider_failure_blocks_quality_gate(extra):
    """Quality percentages cannot hide isolation, transport or recovery failures."""
    assert score_run(dataset(), evidence(**extra))["status"] == "FAIL"


def test_empty_or_duplicate_gold_is_an_error():
    """Invalid evaluation labels fail before scoring."""
    with pytest.raises(ValueError):
        score_run({"cases": [], "queries": []}, evidence())
    gold = dataset()
    gold["cases"].append(gold["cases"][0])
    with pytest.raises(ValueError):
        score_run(gold, evidence())


def test_word_substrings_do_not_count_as_gold_facts():
    """An unrelated Latin word is not an occurrence of a short gold term."""
    gold = dataset()
    gold["cases"][0]["expected"][0]["terms"] = [["Rust"]]
    assert (
        score_run(gold, evidence(extractions={"fact": ["trust"], "transient": []}))["precision"]
        == 0
    )


def test_overlapping_annotations_use_maximum_matching():
    """Input ordering cannot lower or inflate a valid concept match count."""
    gold = dataset()
    gold["cases"][0]["expected"] = [
        {"id": "wal", "terms": [["Alpha"]]},
        {"id": "sql", "terms": [["Alpha"], ["SQLite"]]},
    ]
    result = score_run(
        gold, evidence(extractions={"fact": ["Alpha SQLite", "Alpha"], "transient": []})
    )
    assert result["precision"] == result["positive_case_recall"] == 1


def test_corpus_is_fixed_complete_and_annotated():
    """The multi-turn corpus has positive/negative turns across three scopes."""
    import json
    from pathlib import Path

    from qdrant_memory.quality_metrics import validate_gold

    data = json.loads((Path(__file__).parent / "quality" / "lifecycle-v1.json").read_text())
    assert len(data["cases"]) == 24
    assert len(validate_gold(data)) == 16
    assert len(data["queries"]) == 32
    assert {case["route"] for case in data["cases"]} == {"native", "project", "pi"}


def test_mutation_scenarios_require_every_check_and_zero_obsolete_hits():
    """Quality cannot pass after an omitted scenario, a stale fact or failed revocation."""
    from functools import partial

    from qdrant_memory.quality_metrics import MUTATION_CHECKS, score_mutations

    score_mutations = partial(score_mutations, writer_agent="pi", reader_agent="hermes")
    checks = dict.fromkeys(MUTATION_CHECKS, True)
    assert score_mutations(checks, obsolete_hits=0, observed_hits=1)["status"] == "PASS"
    for key in MUTATION_CHECKS:
        assert (
            score_mutations(
                {k: v for k, v in checks.items() if k != key}, obsolete_hits=0, observed_hits=1
            )["status"]
            == "FAIL"
        )
        assert (
            score_mutations({**checks, key: False}, obsolete_hits=0, observed_hits=1)["status"]
            == "FAIL"
        )
    assert score_mutations(checks, obsolete_hits=1, observed_hits=2)["status"] == "FAIL"
    assert score_mutations(checks, obsolete_hits=0, observed_hits=0)["status"] == "FAIL"


def test_cross_agent_handoff_requires_distinct_actor_evidence():
    """A claimed handoff without logical-Agent identities must fail closed."""
    from qdrant_memory.quality_metrics import MUTATION_CHECKS, score_mutations

    checks = dict.fromkeys(MUTATION_CHECKS, True)
    assert score_mutations(checks, obsolete_hits=0, observed_hits=1)["status"] == "FAIL"
    assert (
        score_mutations(
            checks, obsolete_hits=0, observed_hits=1, writer_agent="pi", reader_agent="pi"
        )["status"]
        == "FAIL"
    )
