"""Evaluate real embedding retrieval against a labeled synthetic pilot dataset."""

import argparse
import json
import platform
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qdrant_memory.config import embedding_config, load_config
from qdrant_memory.embedding import build_embedder
from qdrant_memory.models import Scope, normalize, payload, point_id
from qdrant_memory.qdrant_store import QdrantStore, build_client
from qdrant_memory.quality_metrics import latency_summary


def validate_dataset(dataset):
    """Reject unusable labels and empty samples before any store mutation."""
    if not isinstance(dataset.get("queries"), list) or not dataset["queries"]:
        raise ValueError("Dataset queries must be nonempty")
    if not isinstance(dataset.get("memories"), list) or not dataset["memories"]:
        raise ValueError("Dataset memories must be nonempty")
    allow_abstention = dataset.get("allow_abstention") is True
    identifiers = []
    for memory in dataset["memories"]:
        if not isinstance(memory.get("id"), str) or not memory["id"]:
            raise ValueError("Dataset memory IDs must be nonempty strings")
        identifiers.append(memory["id"])
    known = set(identifiers)
    if len(known) != len(identifiers):
        raise ValueError("Dataset memory IDs must be unique")
    for query in dataset["queries"]:
        if not isinstance(query.get("query"), str) or not normalize(query["query"]):
            raise ValueError("Dataset query text must be nonempty")
        category = query.get("category")
        if category is not None and (not isinstance(category, str) or not category.strip()):
            raise ValueError("Dataset query category must be a nonempty string")
        relevant = query.get("relevant_ids")
        if not isinstance(relevant, list) or (not relevant and not allow_abstention):
            raise ValueError("Dataset relevant_ids must be nonempty")
        if not all(isinstance(identifier, str) and identifier in known for identifier in relevant):
            raise ValueError("Dataset relevant_ids contains an unknown memory ID")


def evaluate(dataset, store, scope):
    """Measure dense retrieval quality and latency against labeled memory/query pairs."""
    validate_dataset(dataset)
    rows = [
        (point_id(scope, "manual_tool", m["id"]), payload(m["text"], scope, "manual_tool"))
        for m in dataset["memories"]
    ]
    names = {point_id(scope, "manual_tool", m["id"]): m["id"] for m in dataset["memories"]}
    store.upsert(rows)
    samples, abstentions, latencies = [], [], []
    for query in dataset["queries"]:
        started = time.monotonic()
        hits = store.search(query["query"], scope, top_k=10)
        latencies.append((time.monotonic() - started) * 1000)
        retrieved = [names[str(h.id)] for h in hits]
        relevant = set(query["relevant_ids"])
        if not relevant:
            # No memory answers this query. Record the top score so separability can be
            # studied, but never score it as a recall miss. No abstention threshold exists.
            abstentions.append(
                {
                    "category": query.get("category") or "abstention",
                    "top1_score": hits[0].score if hits else None,
                }
            )
            continue
        sample = {"category": query.get("category") or "uncategorized"}
        for k in (1, 5, 10):
            sample[f"recall_at_{k}"] = len(relevant & set(retrieved[:k])) / len(relevant)
        for k in (1, 5):
            sample[f"precision_at_{k}"] = len(relevant & set(retrieved[:k])) / min(k, len(rows))
        sample["mrr"] = next(
            (1 / (i + 1) for i, identifier in enumerate(retrieved) if identifier in relevant), 0
        )
        samples.append(sample)
    latencies.sort()

    def summarize(items):
        """Summarize retrieval quality for the supplied labeled query samples."""
        return {
            "queries": len(items),
            **{
                metric: sum(item[metric] for item in items) / len(items)
                for metric in (
                    "recall_at_1",
                    "recall_at_5",
                    "recall_at_10",
                    "precision_at_1",
                    "precision_at_5",
                    "mrr",
                )
            },
        }

    categories = sorted({sample["category"] for sample in samples})
    timing = latency_summary(latencies)
    scores = sorted(item["top1_score"] for item in abstentions if item["top1_score"] is not None)
    score_summary = latency_summary(scores)

    return {
        "memories": len(rows),
        **summarize(samples),
        "labeled_queries": len(samples),
        "abstention_queries": len(abstentions),
        "abstention_top1_score": {
            "samples": score_summary["samples"],
            "p50": score_summary["p50_ms"],
            "p95": score_summary["p95_ms"],
            "max": scores[-1] if scores else None,
            "note": "No calibrated abstention threshold exists; these scores are observational.",
        },
        "by_category": {
            category: summarize([sample for sample in samples if sample["category"] == category])
            for category in categories
        },
        **timing,
        "search_latency_ms_p50": timing["p50_ms"],
        "search_latency_ms_p95": timing["p95_ms"],
        "search_latency_ms_max": latencies[-1] if latencies else None,
        "latency_method": "nearest-rank p50/p95 over per-query search wall time",
    }


def main():
    """Run a UTF-8 dataset evaluation in a disposable store and print JSON."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "tests/fixtures/retrieval.json",
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="hermes-qdrant-evaluation-") as home:
        cfg = load_config(
            home, {"qdrant": {"mode": "embedded"}, "search": {"top_k": 10, "candidate_k": 24}}
        )
        embedder = build_embedder(None, cfg)
        store = QdrantStore(build_client(cfg), cfg, embedder)
        try:
            store.initialize()
            result = evaluate(
                json.loads(args.dataset.read_text(encoding="utf-8")),
                store,
                Scope("evaluation", "hermes"),
            )
            result.update(
                embedding={
                    k: embedding_config(cfg)[k] for k in ("provider", "model", "dimensions")
                },
                embedding_fingerprint=embedder.fingerprint,
                platform=platform.platform(),
                python=platform.python_version(),
                dataset=str(args.dataset.name),
                note=(
                    "Synthetic labeled retrieval cases; results are not a production recall "
                    "guarantee."
                ),
            )
            print(json.dumps(result, indent=2))
        finally:
            store.close()
            embedder.close()


if __name__ == "__main__":
    main()
