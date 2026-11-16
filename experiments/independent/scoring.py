"""Per-query scoring and paired fact-group uncertainty (no model dependencies)."""

import math
import random
from collections import defaultdict
from statistics import mean


def percentile(values, fraction):
    """Select the nearest-rank empirical percentile."""
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def score(q, ranked):
    """Measure direct answers, temporal ordering and interference."""
    ranked = ranked[:8]
    assert len(ranked) == len(set(ranked)), "duplicate retrieved ID"
    correct, conflict = set(q["correct_ids"]), set(q["conflict_ids"])
    positions = {mid: i for i, mid in enumerate(ranked)}
    present = [positions[x] for x in correct if x in positions]
    wrong = [positions[x] for x in conflict if x in positions]
    dcg = sum(1 / math.log2(i + 2) for i, mid in enumerate(ranked) if mid in correct)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(8, len(correct))))
    return dict(
        recall=len(present) / len(correct) if correct else None,
        ndcg=dcg / ideal if ideal else None,
        current_present=float(bool(present)),
        stale_before=float(
            bool(correct)
            and bool(wrong)
            and (len(present) < len(correct) or min(wrong) < max(present))
        ),
        historical_accuracy=float(
            bool(correct)
            and len(present) == len(correct)
            and (not wrong or max(present) < min(wrong))
        ),
        interference=float(bool(ranked)),
        returned_count=len(ranked),
        related_count=len(set(ranked) & set(q["related_ids"])),
    )


def paired_interval(rows, key, draws=10000):
    """Bootstrap paired differences by independent fact group."""
    grouped = defaultdict(list)
    for row in rows:
        d, h = row["dense"][key], row["hybrid"][key]
        if d is not None and h is not None:
            grouped[row["fact_group_id"]].append(h - d)
    groups = list(grouped.values())
    if not groups:
        return None
    rng = random.Random(152)
    samples = []
    for _ in range(draws):
        picked = rng.choices(groups, k=len(groups))
        samples.append(sum(sum(g) for g in picked) / sum(len(g) for g in picked))
    return dict(
        delta=mean(x for g in groups for x in g),
        ci95=[percentile(samples, 0.025), percentile(samples, 0.975)],
        fact_groups=len(groups),
    )


def summarize(rows):
    """Aggregate quality while retaining no-answer diagnostics."""
    output = {}
    for tag in ["all"] + sorted({t for r in rows for t in r["tags"]}):
        subset = [r for r in rows if tag == "all" or tag in r["tags"]]
        answered = [r for r in subset if r["correct_ids"]]
        record = dict(
            queries=len(subset),
            answered_queries=len(answered),
            fact_groups=len({r["fact_group_id"] for r in subset}),
        )
        for path in ("dense", "hybrid"):
            record[path] = {
                key: mean(r[path][key] for r in answered) if answered else None
                for key in ("recall", "ndcg")
            }
        record["paired"] = {key: paired_interval(answered, key) for key in ("recall", "ndcg")}
        differences = [r["hybrid"]["recall"] - r["dense"]["recall"] for r in answered]
        record["win_tie_loss"] = [
            sum(x > 1e-12 for x in differences),
            sum(abs(x) <= 1e-12 for x in differences),
            sum(x < -1e-12 for x in differences),
        ]
        extra = (
            ["stale_before", "current_present"]
            if tag == "stale_fact"
            else ["historical_accuracy"]
            if tag == "historical"
            else ["interference", "returned_count"]
            if tag == "no_answer"
            else []
        )
        for key in extra:
            record[key] = {path: mean(r[path][key] for r in subset) for path in ("dense", "hybrid")}
            record[key]["paired"] = paired_interval(subset, key)
        output[tag] = record
    return output


def latency_summary(samples):
    """Calculate timing quantiles and paired cluster intervals."""
    out = {}
    for kind in ("e2e", "retrieval"):
        out[kind] = {}
        for round_id in ["pooled"] + sorted({s["round"] for s in samples}):
            rows = [s for s in samples if round_id == "pooled" or s["round"] == round_id]
            stats = {
                path: dict(
                    n=len(rows),
                    p50_ms=percentile([r[kind][path] for r in rows], 0.5),
                    p95_ms=percentile([r[kind][path] for r in rows], 0.95),
                )
                for path in ("dense", "hybrid")
            }
            d, h = stats["dense"]["p95_ms"], stats["hybrid"]["p95_ms"]
            grouped = defaultdict(list)
            for row in rows:
                grouped[row["fact_group_id"]].append(row)
            groups = list(grouped.values())
            rng = random.Random(152)
            ratios = []
            for _ in range(2000):
                picked = [r for g in rng.choices(groups, k=len(groups)) for r in g]
                ratios.append(
                    percentile([r[kind]["hybrid"] for r in picked], 0.95)
                    / percentile([r[kind]["dense"] for r in picked], 0.95)
                )
            out[kind][str(round_id)] = dict(
                **stats,
                delta_ms=h - d,
                delta_percent=(h / d - 1) * 100,
                p95_ratio_ci95=[percentile(ratios, 0.025), percentile(ratios, 0.975)],
                p50_delta_ms=stats["hybrid"]["p50_ms"] - stats["dense"]["p50_ms"],
                p50_delta_percent=(stats["hybrid"]["p50_ms"] / stats["dense"]["p50_ms"] - 1) * 100,
            )
    return out
