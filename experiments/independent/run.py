"""Frozen real-model evaluation. Run from the repository root; no Runtime imports."""

import argparse
import json
import platform
import subprocess
import time
import traceback
import uuid
from pathlib import Path

from dataset import ROOT, digest, inventory, load_dataset
from decision import decide
from scoring import latency_summary, score, summarize
from server import CONFIG, PATHS, Embedding, Server


def verify_freeze():
    """Verify immutable data, review and configuration."""
    frozen = json.loads((ROOT / "freeze.json").read_text())
    for file, sha in frozen["files"].items():
        assert digest(ROOT / file) == sha, f"frozen file changed: {file}"
    assert frozen["config"] == CONFIG, "frozen retrieval config changed"
    review = json.loads((ROOT / "review.json").read_text())
    assert review["status"] == "approved" and review["reviewer"], "annotation review required"
    assert review["dataset_sha256"] == digest(ROOT / "dataset.json")
    _, queries = load_dataset()
    expanded = "".join(json.dumps(q, ensure_ascii=False, sort_keys=True) + "\n" for q in queries)
    assert expanded == (ROOT / "annotations.jsonl").read_text(), (
        "expanded annotations differ from authoring source"
    )
    assert set(review["query_ids"]) == {q["query_id"] for q in queries}, (
        "incomplete annotation review"
    )
    return frozen


def timed(call):
    """Measure a complete call with a monotonic clock."""
    start = time.perf_counter()
    value = call()
    return value, (time.perf_counter() - start) * 1000


def quality(server, embedding, queries, forbidden):
    """Score both retrieval paths using identical query vectors."""
    rows = []
    for i, q in enumerate(queries):
        vector = embedding.embed(q["query"])
        row = dict(q)
        for key, path in zip(("dense", "hybrid"), PATHS):
            ranked = server.search(q["query"], vector, q["scope"], path)
            assert not forbidden.intersection(ranked), "DELETED_ID_LEAK"
            row[key + "_ids"] = ranked
            row[key] = score(q, ranked)
        rows.append(row)
        print(f"quality {i + 1}/{len(queries)} {q['query_id']}", flush=True)
    return rows


def latency(server, embedding, queries, rounds, minimum_pairs):
    """Measure alternating warmed pairs with and without embedding."""
    vectors = {q["query_id"]: embedding.embed(q["query"]) for q in queries}
    for _ in range(2):
        for q in queries:
            for path in PATHS:
                server.search(q["query"], embedding.embed(q["query"]), q["scope"], path)
    samples = []
    count = max(minimum_pairs, len(queries))
    for round_id in range(rounds):
        for i in range(count):
            q = queries[i % len(queries)]
            row = dict(
                round=round_id,
                query_id=q["query_id"],
                fact_group_id=q["fact_group_id"],
                e2e={},
                retrieval={},
            )
            order = list(zip(("dense", "hybrid"), PATHS))
            if (i + round_id) % 2:
                order.reverse()
            for kind in ("e2e", "retrieval"):
                for key, path in order:

                    def search():
                        vector = (
                            embedding.embed(q["query"]) if kind == "e2e" else vectors[q["query_id"]]
                        )
                        return server.search(q["query"], vector, q["scope"], path)

                    _, row[kind][key] = timed(search)
            samples.append(row)
        print(f"latency round {round_id + 1}/{rounds}: {count} pairs", flush=True)
    return samples


def report(result):
    """Render quality, latency, uncertainty and failures."""
    lines = [
        "# Independent BM25 evaluation",
        "",
        f"Execution: **{result['execution_status']}**; experiment: **{result['decision']['verdict']}**.",
        f"Split: {result.get('split')}; commit: `{result.get('commit')}`; run: {result.get('ci_run', 'local')}.",
        f"Freeze SHA256: `{result.get('freeze_sha256')}`.",
        "",
        "Original Chinese +10pp check is retained separately; this report uses the preregistered opt-in plan.",
        "",
    ]
    if "error" in result:
        lines += [result["error"], ""]
    if "quality" in result:
        lines += [
            "| Scenario | N / groups | Dense Recall / nDCG | Hybrid Recall / nDCG | Recall delta [95% CI] | W/T/L |",
            "|---|---:|---:|---:|---|---|",
        ]
        for tag, stats in result["quality"].items():
            if not stats["answered_queries"]:
                continue
            d, h, ci = stats["dense"], stats["hybrid"], stats["paired"]["recall"]
            lines.append(
                f"| {tag} | {stats['queries']} / {stats['fact_groups']} | {d['recall']:.3f} / {d['ndcg']:.3f} | {h['recall']:.3f} / {h['ndcg']:.3f} | {ci['delta']:+.3f} [{ci['ci95'][0]:+.3f}, {ci['ci95'][1]:+.3f}] | {stats['win_tie_loss']} |"
            )
        lines += [
            "",
            "## Time and no-answer diagnostics",
            "",
            "```json",
            json.dumps(
                {
                    tag: {
                        k: v
                        for k, v in result["quality"].get(tag, {}).items()
                        if k
                        in [
                            "stale_before",
                            "current_present",
                            "historical_accuracy",
                            "interference",
                            "returned_count",
                        ]
                    }
                    for tag in ["stale_fact", "historical", "no_answer"]
                },
                indent=2,
            ),
            "```",
            "",
            "## Warm latency",
            "",
            "| Timing / round | N pairs | Dense p50 / p95 ms | Hybrid p50 / p95 ms | p95 delta ms / % | ratio 95% CI |",
            "|---|---:|---:|---:|---:|---|",
        ]
        for kind, rounds in result.get("latency", {}).items():
            for rnd, stats in rounds.items():
                d, h = stats["dense"], stats["hybrid"]
                lines.append(
                    f"| {kind} / {rnd} | {d['n']} | {d['p50_ms']:.1f} / {d['p95_ms']:.1f} | {h['p50_ms']:.1f} / {h['p95_ms']:.1f} | {stats['delta_ms']:+.1f} / {stats['delta_percent']:+.1f}% | {stats['p95_ratio_ci95']} |"
                )
        lines += ["", "## Cases to review", ""]
        failures = sorted(
            result["rows"],
            key=lambda r: (
                (r["hybrid"]["recall"] or 0) - (r["dense"]["recall"] or 0),
                (r["hybrid"]["ndcg"] or 0) - (r["dense"]["ndcg"] or 0),
            ),
        )
        failures = [
            r
            for r in failures
            if r["correct_ids"] and (r["hybrid"]["ndcg"] < 1 or r["hybrid"]["stale_before"])
        ][:8]
        if not failures:
            lines.append(
                "No answered-query failure at top 8 in this run; this does not establish generalization."
            )
        for r in failures:
            lines += [
                f"- `{r['query_id']}` {r['query']} — {r['rationale']}",
                f"  Correct: {r['correct_ids']}; conflict: {r['conflict_ids']}",
                f"  Dense: {r['dense_ids']}",
                f"  Hybrid: {r['hybrid_ids']}",
            ]
        lines += [
            "",
            "Original Chinese +10pp point check: "
            + (
                "PASS"
                if result["quality"]
                .get("chinese", {})
                .get("paired", {})
                .get("recall", {})
                .get("delta", -1)
                >= 0.10
                else "FAIL"
            )
            + ". This is not the warm-p95 decision.",
        ]
    lines += [
        "",
        "## Decision details",
        "",
        "```json",
        json.dumps(result["decision"], indent=2),
        "```",
        "",
        "Recommendation: "
        + (
            "Consider a separate Server-only, default-off experiment next; do not integrate Runtime in this PR."
            if result["decision"]["verdict"] == "GO"
            else "Keep this Draft experiment isolated; resolve the reported uncertainty or regressions before proposing a Server-only default-off entry point."
        ),
        "",
    ]
    return "\n".join(lines)


def main():
    """Execute the experiment and persist attributed results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["dev", "holdout"], required=True)
    parser.add_argument("--server", default="http://127.0.0.1:16333")
    parser.add_argument("--ollama", default="http://127.0.0.1:11434")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--latency-pairs", type=int, default=100)
    parser.add_argument(
        "--latency-queries",
        type=int,
        default=0,
        help="0 uses all split queries; smaller values are exploratory",
    )
    parser.add_argument("--lifecycle-report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import os

    result = dict(
        execution_status="ERROR",
        split=args.split,
        config=CONFIG,
        commit=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        ci_run=os.getenv("GITHUB_RUN_ID", "local"),
        platform=platform.platform(),
        freeze_sha256=digest(ROOT / "freeze.json")
        if (ROOT / "freeze.json").exists()
        else "MISSING",
        source_dirty=bool(
            subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()
        ),
    )
    server = None
    try:
        verify_freeze()
        if args.lifecycle_report:
            result["lifecycle"] = json.loads(args.lifecycle_report.read_text())
            assert result["lifecycle"]["image"] == CONFIG["image"]
            assert result["lifecycle"]["status"] == "PASS", "LIFECYCLE_FAILURE"
        memories, queries = load_dataset()
        result["inventory"] = inventory(queries)
        queries = [q for q in queries if q["split"] == args.split]
        assert args.rounds > 0 and args.latency_pairs > 0
        embedding = Embedding(args.ollama)
        result["embedding"] = embedding.metadata
        server = Server(args.server, "bm25_ind_quality_" + uuid.uuid4().hex)
        result["qdrant"] = server.http.get("/").raise_for_status().json()
        assert result["qdrant"]["version"] == "1.20.0", "unexpected Qdrant version"
        result["qdrant_image_digest"] = os.getenv("QDRANT_IMAGE_DIGEST", "UNVERIFIED")
        if result["qdrant_image_digest"] != CONFIG["image"].split("@", 1)[1]:
            raise LookupError("UNVERIFIED: Qdrant image digest missing or mismatched")
        server.create(2560)
        for i, memory in enumerate(memories):
            server.upsert(memory, embedding.embed(memory["text"]))
            if memory["deleted"]:
                server.delete(memory["id"])
            if i % 25 == 0:
                print(f"corpus {i + 1}/{len(memories)}", flush=True)
        result["collection"] = server.collection
        result["expanded_queries"] = queries
        q = queries[0]
        result["cold_path"] = {
            path: timed(
                lambda: server.search(q["query"], embedding.embed(q["query"]), q["scope"], path)
            )[1]
            for path in PATHS
        }
        result["cold_path_note"] = (
            "first search call after ingest, not a process/model-cold measurement"
        )
        result["rows"] = quality(
            server, embedding, queries, {m["id"] for m in memories if m["deleted"]}
        )
        result["quality"] = summarize(result["rows"])
        latency_queries = queries[: args.latency_queries] if args.latency_queries else queries
        result["latency_samples"] = latency(
            server, embedding, latency_queries, args.rounds, args.latency_pairs
        )
        result["latency"] = latency_summary(result["latency_samples"])
        result["execution_status"] = "SUCCESS"
        if args.latency_queries:
            result["execution_status"] = "EXPLORATORY_SUBSET"
    except LookupError as error:
        result.update(execution_status="UNVERIFIED", error=str(error))
    except Exception as error:
        result.update(error=f"{type(error).__name__}: {error}", traceback=traceback.format_exc())
        result["safety_failure"] = any(
            marker in str(error) for marker in ("LEAK", "CROSS_SCOPE", "LIFECYCLE_FAILURE")
        )
    finally:
        if server:
            if result["execution_status"] in {"SUCCESS", "EXPLORATORY_SUBSET"}:
                try:
                    server.request("DELETE")
                except Exception as error:
                    result.update(execution_status="ERROR", error=f"cleanup failed: {error}")
            server.close()
        result["decision"] = decide(result)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        args.output.with_suffix(".md").write_text(report(result))
        print(f"execution={result['execution_status']} experiment={result['decision']['verdict']}")
    return (
        0
        if result["execution_status"] in {"SUCCESS", "EXPLORATORY_SUBSET"}
        and not result.get("safety_failure")
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
