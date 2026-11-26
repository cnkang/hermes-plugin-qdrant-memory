"""Compare broad and focused queries on one corpus without changing retrieval defaults."""

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

ROOT = Path(__file__).resolve().parents[1]
VARIANTS = ("broad", "focused")


def validate(corpus, variants):
    """Reject unusable labels before any store mutation."""
    memories = corpus.get("memories")
    if not isinstance(memories, list) or not memories:
        raise ValueError("Corpus memories must be nonempty")
    identifiers = {memory["id"] for memory in memories}
    for query in variants.get("queries", []):
        if not isinstance(query.get("relevant_ids"), list) or not query["relevant_ids"]:
            raise ValueError("Each variant query needs relevant IDs")
        if not set(query["relevant_ids"]) <= identifiers:
            raise ValueError(f"{query.get('id')}: unknown relevant ID")
        for name in VARIANTS:
            if not isinstance(query.get(name), str) or not normalize(query[name]):
                raise ValueError(f"{query.get('id')}: missing {name} text")


def metrics(retrieved, relevant):
    """Return Recall@1/5 and MRR for one retrieved list."""
    return {
        "recall_at_1": len(relevant & set(retrieved[:1])) / len(relevant),
        "recall_at_5": len(relevant & set(retrieved[:5])) / len(relevant),
        "mrr": next(
            (1 / (i + 1) for i, identifier in enumerate(retrieved) if identifier in relevant), 0
        ),
    }


def main(argv=None):
    """Run each variant against a disposable store and report per-variant metrics."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus", type=Path, default=ROOT / "tests/fixtures/retrieval-scale-v1.json"
    )
    parser.add_argument(
        "--variants", type=Path, default=ROOT / "tests/fixtures/query-construction-v1.json"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    variants = json.loads(args.variants.read_text(encoding="utf-8"))
    validate(corpus, variants)
    scope = Scope("query-construction", "hermes")
    with tempfile.TemporaryDirectory(prefix="hermes-qdrant-query-variants-") as home:
        cfg = load_config(
            home, {"qdrant": {"mode": "embedded"}, "search": {"top_k": 10, "candidate_k": 24}}
        )
        embedder = build_embedder(None, cfg)
        store = QdrantStore(build_client(cfg), cfg, embedder)
        try:
            store.initialize()
            rows = [
                (
                    point_id(scope, "manual_tool", memory["id"]),
                    payload(memory["text"], scope, "manual_tool"),
                )
                for memory in corpus["memories"]
            ]
            names = {
                point_id(scope, "manual_tool", memory["id"]): memory["id"]
                for memory in corpus["memories"]
            }
            store.upsert(rows)
            results = {name: {"samples": [], "latencies": []} for name in VARIANTS}
            detail = []
            for query in variants["queries"]:
                relevant = set(query["relevant_ids"])
                row = {"id": query["id"]}
                for name in VARIANTS:
                    started = time.monotonic()
                    hits = store.search(query[name], scope, top_k=10)
                    elapsed = (time.monotonic() - started) * 1000
                    retrieved = [names[str(hit.id)] for hit in hits]
                    sample = metrics(retrieved, relevant)
                    results[name]["samples"].append(sample)
                    results[name]["latencies"].append(elapsed)
                    row[name] = {
                        "query": query[name],
                        "top1": retrieved[0] if retrieved else None,
                        **sample,
                    }
                detail.append(row)
            summary = {}
            for name, data in results.items():
                summary[name] = {
                    "queries": len(data["samples"]),
                    **{
                        metric: sum(sample[metric] for sample in data["samples"])
                        / len(data["samples"])
                        for metric in ("recall_at_1", "recall_at_5", "mrr")
                    },
                    **latency_summary(data["latencies"]),
                }
            report = {
                "corpus_memories": len(rows),
                "queries": len(variants["queries"]),
                "variants": summary,
                "detail": detail,
                "embedding": {
                    key: embedding_config(cfg)[key] for key in ("provider", "model", "dimensions")
                },
                "embedding_fingerprint": embedder.fingerprint,
                "platform": platform.platform(),
                "python": platform.python_version(),
                "retrieval": "unchanged dense pipeline, top_k 10, no reranker",
                "note": "Synthetic corpus and queries; no answer terms appear in either variant.",
            }
            text = json.dumps(report, indent=2, ensure_ascii=False)
            if args.output:
                args.output.write_text(text + "\n", encoding="utf-8")
                print(json.dumps({"variants": summary, "out": str(args.output)}))
            else:
                print(text)
        finally:
            store.close()
            embedder.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
