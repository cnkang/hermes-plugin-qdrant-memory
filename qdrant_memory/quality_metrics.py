"""Fail-closed scoring for anonymous lifecycle evaluations; no memory I/O."""

import math
import re
import unicodedata

THRESHOLDS = {"precision": 0.95, "positive_case_recall": 0.9, "recall_at_5": 0.9, "mrr": 0.8}


def matches(text, fact):
    """Match all annotated concept groups without counting Latin word substrings."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return all(
        any(
            re.search(
                r"(?<![a-z0-9_])"
                + re.escape(unicodedata.normalize("NFKC", term).casefold())
                + r"(?![a-z0-9_])",
                text,
            )
            for term in alternatives
        )
        for alternatives in fact["terms"]
    )


def validate_gold(dataset):
    """Reject ambiguous IDs, missing positive examples and malformed annotations."""
    cases, queries = dataset.get("cases"), dataset.get("queries")
    if not isinstance(cases, list) or not cases or not isinstance(queries, list) or not queries:
        raise ValueError("Nonempty cases and retrieval queries are required")
    identifiers, facts = set(), {}
    for case in cases:
        key = case.get("id")
        expected = case.get("expected")
        if (
            not isinstance(key, str)
            or not key
            or key in identifiers
            or not isinstance(expected, list)
        ):
            raise ValueError("Unique case IDs and explicit expected facts are required")
        identifiers.add(key)
        for fact in expected:
            key, terms = fact.get("id"), fact.get("terms")
            if not isinstance(key, str) or not key or key in facts:
                raise ValueError("Unique fact IDs are required")
            if (
                not isinstance(terms, list)
                or not terms
                or any(
                    not isinstance(group, list)
                    or not group
                    or any(not isinstance(term, str) or not term.strip() for term in group)
                    for group in terms
                )
            ):
                raise ValueError("Each fact requires nonempty concept/synonym groups")
            facts[key] = fact
    identifiers = set()
    for query in queries:
        key = query.get("id")
        if (
            not isinstance(key, str)
            or not key
            or key in identifiers
            or query.get("expected") not in facts
        ):
            raise ValueError("Unique query IDs and known expected facts are required")
        identifiers.add(key)
    return facts


def _matched_count(rows, facts):
    """Use maximum matching so duplicate rows and overlapping labels cannot inflate TP."""
    assigned = {}

    def assign(row, seen):
        for index, fact in enumerate(facts):
            if index in seen or not matches(rows[row], fact):
                continue
            seen.add(index)
            if index not in assigned or assign(assigned[index], seen):
                assigned[index] = row
                return True
        return False

    return sum(assign(row, set()) for row in range(len(rows)))


def score_run(dataset, evidence):
    """Require complete extraction/retrieval, zero leakage and confirmed replay/restart."""
    validate_gold(dataset)
    extractions = evidence.get("extractions", {})
    retrievals = evidence.get("retrievals", {})
    if not isinstance(extractions, dict):
        extractions = {}
    if not isinstance(retrievals, dict):
        retrievals = {}
    complete = set(extractions) == {case["id"] for case in dataset["cases"]}
    complete &= set(retrievals) == {query["id"] for query in dataset["queries"]}
    true, false, positives, recalled, negatives = 0, 0, 0, 0, 0
    for case in dataset["cases"]:
        rows = extractions.get(case["id"], [])
        if not isinstance(rows, list) or any(not isinstance(row, str) for row in rows):
            rows, complete = [], False
        expected = case["expected"]
        matched = _matched_count(rows, expected)
        true += matched
        false += len(rows) - matched
        negatives += not expected and not rows
        if expected:
            positives += 1
            recalled += matched == len(expected)
    ranks = []
    for query in dataset["queries"]:
        rows = retrievals.get(query["id"], [])
        if not isinstance(rows, list) or any(
            row is not None and not isinstance(row, str) for row in rows
        ):
            rows, complete = [], False
        top = rows[:5]
        ranks.append(1 / (top.index(query["expected"]) + 1) if query["expected"] in top else 0)
    result = {
        "precision": true / (true + false) if true + false else 0,
        "positive_case_recall": recalled / positives,
        "recall_at_5": sum(rank > 0 for rank in ranks) / len(ranks),
        "mrr": sum(ranks) / len(ranks),
        "rank_limit": 5,
        "true_positives": true,
        "false_positives": false,
        "negative_cases_rejected": negatives,
        "complete": complete,
        "thresholds": THRESHOLDS.copy(),
    }
    integrity = all(
        type(evidence.get(key)) is int and evidence[key] == 0
        for key in ("scope_leaks", "provider_failures")
    ) and all(evidence.get(key) is True for key in ("replay_safe", "restart_safe"))
    passing = (
        complete and integrity and all(result[key] >= value for key, value in THRESHOLDS.items())
    )
    result["status"] = "PASS" if passing else "FAIL"
    return result


def latency_summary(values):
    """Report nearest-rank p95; an unmeasured series never becomes zero latency."""
    if not values:
        return {"samples": 0, "p50_ms": None, "p95_ms": None}
    ordered = sorted(values)
    return {
        "samples": len(values),
        "p50_ms": ordered[(len(ordered) - 1) // 2],
        "p95_ms": ordered[math.ceil(0.95 * len(ordered)) - 1],
    }


def durable_snapshot(hits):
    """Compare every returned payload field; similarity scores are computed, not stored."""
    result = {}
    for hit in hits:
        identity = hit["id"]
        if identity in result:
            raise ValueError("Duplicate identity in restart evidence")
        result[identity] = {key: value for key, value in hit.items() if key != "score"}
    return result


MUTATION_CHECKS = (
    "same_agent_continuation",
    "supersession",
    "multilingual",
    "cross_agent_handoff",
    "readonly_denied",
    "delete_replay",
    "deleted_absent",
    "revocation_denied",
    "cas_one_winner",
    "restart_durable",
    "restart_no_model_calls",
)


def score_mutations(checks, *, obsolete_hits, observed_hits):
    """Require all mutation/isolation scenarios and observed absence of obsolete recall."""
    complete = set(checks) == set(MUTATION_CHECKS)
    valid_counts = (
        type(obsolete_hits) is int
        and type(observed_hits) is int
        and 0 <= obsolete_hits <= observed_hits
        and observed_hits > 0
    )
    passing = complete and all(value is True for value in checks.values())
    passing = passing and valid_counts and obsolete_hits == 0
    return {
        "status": "PASS" if passing else "FAIL",
        "complete": complete,
        "checks": checks,
        "obsolete_hits": obsolete_hits,
        "observed_hits": observed_hits,
        "obsolete_hit_rate": obsolete_hits / observed_hits if valid_counts else None,
    }
