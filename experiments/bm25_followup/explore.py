"""Bounded development-only fusion, tokenization and transport diagnostics."""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "independent"))
from dataset import digest, load_dataset
from run import verify_freeze
from scoring import percentile, score, summarize
from server import CONFIG, Embedding, Server, fixture_vector, scope_filter

VARIANTS = {"rrf_1_1": [1, 1], "rrf_2_1": [2, 1], "rrf_4_1": [4, 1]}
PROJECTION = {"include": ["memory_id", "user_id", "agent_id"]}


class DiagnosticServer(Server):
    """Capture server and client timing without editing the frozen adapter."""

    def create_index(self, dimensions, tokenizer="multilingual", minimum=2, maximum=15):
        """Create an owned collection with an explicit probe index configuration."""
        self.request("PUT", json={"vectors": {"dense": {"size": dimensions, "distance": "Cosine"}}})
        self.request(
            "PUT",
            "/index?wait=true",
            json={
                "field_name": "text",
                "field_schema": {
                    "type": "text",
                    "tokenizer": tokenizer,
                    "min_token_len": minimum,
                    "max_token_len": maximum,
                    "lowercase": True,
                    "enable_hnsw": True,
                    "scoring": {"type": "bm25"},
                },
            },
        )

    def query(self, text, vector, scope, weights=None, sparse=False, projection=True, limit=24):
        """Execute a query with separately measured HTTP, decoding and server time."""
        start = time.perf_counter()
        filt = scope_filter(scope)
        body = dict(filter=filt, limit=limit, with_payload=projection, with_vector=False)
        if weights is not None:
            body.update(
                prefetch=[
                    dict(query=vector, using="dense", limit=24, filter=filt),
                    dict(query={"text": text}, using="text", limit=24, filter=filt),
                ],
                query={"rrf": {"k": 60, "weights": weights}},
            )
        else:
            body.update(
                query={"text": text} if sparse else vector, using="text" if sparse else "dense"
            )
        response = self.http.post(self.base + "/points/query", json=body)
        received = time.perf_counter()
        response.raise_for_status()
        data = response.json()
        decoded = time.perf_counter()
        points = data["result"]["points"]
        for point in points:
            assert [point["payload"]["user_id"], point["payload"].get("agent_id")] == scope, (
                "CROSS_SCOPE_LEAK"
            )
        return points, dict(
            client_ms=(decoded - start) * 1000,
            http_ms=(received - start) * 1000,
            decode_ms=(decoded - received) * 1000,
            server_ms=data["time"] * 1000,
            response_bytes=len(response.content),
        )


def ids(points):
    """Extract stable evidence names in ranking order."""
    return [p["payload"]["memory_id"] for p in points]


def probe_tokens(url):
    """Compare actual native token matching on fictional near-neighbour identifiers."""
    pairs = [
        ("model", "qwen3-embedding:4b", "qwen3-embedding:8b"),
        ("pr", "PR #152", "PR #125"),
        ("key", "batch_timeout_seconds", "query_timeout_seconds"),
        ("score", "search.min_score", "retrieval.min_score"),
        ("short", "x", "y"),
    ]
    outputs = {}
    for name, tokenizer, minimum, maximum in [
        ("baseline", "multilingual", 2, 15),
        ("wide", "multilingual", 1, 64),
        ("code", "code", 1, 64),
    ]:
        server = DiagnosticServer(url, "bm25_ind_probe_" + uuid.uuid4().hex)
        try:
            server.create_index(32, tokenizer, minimum, maximum)
            rows = []
            for label, exact, foil in pairs:
                scope = ["fictional-probe", label]
                for mid, text in [("exact", exact), ("foil", foil)]:
                    server.upsert(
                        dict(id=label + "/" + mid, text=text, scope=scope), fixture_vector(text)
                    )
                points, timing = server.query(exact, fixture_vector(exact), scope, sparse=True)
                rows.append(
                    dict(
                        probe=label,
                        query=exact,
                        foil=foil,
                        ranked=ids(points),
                        scores=[p["score"] for p in points],
                        timing=timing,
                    )
                )
            outputs[name] = rows
            if name == "baseline":
                points, _ = server.query(
                    "x", fixture_vector("x"), ["fictional-probe", "short"], weights=[2, 1]
                )
                exact = next(p for p in points if p["payload"]["memory_id"] == "short/exact")
                # 'x' is absent from the baseline text index, leaving dense rank zero.
                assert abs(exact["score"] - 1 / 59.5) < 1e-6, "native RRF weight semantics changed"
        finally:
            server.request("DELETE")
            server.close()
    return outputs


def evaluate(server, queries, vectors):
    """Capture both candidate lists and compare only preregistered fusion weights."""
    rows = []
    for q in queries:
        vector = vectors[q["query_id"]]
        dense, _ = server.query(q["query"], vector, q["scope"])
        sparse, _ = server.query(q["query"], vector, q["scope"], sparse=True)
        union = set(ids(dense)) | set(ids(sparse))
        row = dict(
            q,
            dense_candidates=dense,
            sparse_candidates=sparse,
            candidate_union_recall=len(union & set(q["correct_ids"])) / len(q["correct_ids"])
            if q["correct_ids"]
            else None,
            dense=score(q, ids(dense)[:8]),
            variants={},
        )
        for name, weights in VARIANTS.items():
            fused, timing = server.query(q["query"], vector, q["scope"], weights=weights)
            # Calibrate zero/one-based rank convention against native returned scores.
            errors = {}
            for offset in [0, 1]:
                predicted = {}
                for weight, points in zip(weights, [dense, sparse]):
                    for rank, point in enumerate(points):
                        mid = point["payload"]["memory_id"]
                        predicted[mid] = predicted.get(mid, 0) + 1 / (
                            (rank + 1) / weight + 59 + offset
                        )
                errors[offset] = max(
                    (abs(predicted[p["payload"]["memory_id"]] - p["score"]) for p in fused),
                    default=0,
                )
            convention = min(errors, key=errors.get)
            row["variants"][name] = dict(
                points=fused,
                metrics=score(q, ids(fused)[:8]),
                timing=timing,
                rank_offset=convention if errors[convention] < 1e-5 else "UNKNOWN",
                formula_max_errors=errors,
            )
        row["scalar_controls"] = {}
        for name, weights in VARIANTS.items():
            totals = {}
            for weight, points in zip(weights, [dense, sparse]):
                for rank, point in enumerate(points):
                    mid = point["payload"]["memory_id"]
                    totals[mid] = totals.get(mid, 0) + weight / (60 + rank)
            ranked = sorted(totals, key=lambda mid: (-totals[mid], mid))
            row["scalar_controls"][name] = dict(ranked=ranked, metrics=score(q, ranked[:8]))
        rows.append(row)
    summary = {}
    for name in VARIANTS:
        paired = [dict(r, hybrid=r["variants"][name]["metrics"]) for r in rows]
        summary[name] = summarize(paired)
    controls = {
        name: summarize([dict(r, hybrid=r["scalar_controls"][name]["metrics"]) for r in rows])
        for name in VARIANTS
    }
    return dict(rows=rows, summary=summary, posthoc_scalar_summary=controls)


def latency(server, embedding, queries, vectors, rounds=3):
    """Measure fresh embeddings, server time, payload and connection controls separately."""
    samples = []
    for _ in range(2):
        for q in queries:
            for weights in [None, [1, 1]]:
                server.query(
                    q["query"],
                    vectors[q["query_id"]],
                    q["scope"],
                    weights=weights,
                    limit=8 if weights else 24,
                )
    for rnd in range(rounds):
        for i, q in enumerate(queries):
            order = [("dense", None), ("hybrid", [1, 1])]
            if (i + rnd) % 2:
                order.reverse()
            for mode in ["original", "minimal", "no_keepalive", "e2e"]:
                for path, weights in order:
                    started = time.perf_counter()
                    vector = (
                        embedding.embed(q["query"]) if mode == "e2e" else vectors[q["query_id"]]
                    )
                    embedded = time.perf_counter()
                    original = server.http
                    if mode == "no_keepalive":
                        server.http = httpx.Client(
                            base_url=str(original.base_url),
                            timeout=180,
                            trust_env=False,
                            limits=httpx.Limits(max_keepalive_connections=0),
                        )
                    try:
                        _, timing = server.query(
                            q["query"],
                            vector,
                            q["scope"],
                            weights=weights,
                            limit=8 if weights else 24,
                            projection=PROJECTION if mode == "minimal" else True,
                        )
                    finally:
                        if mode == "no_keepalive":
                            server.http.close()
                            server.http = original
                    timing.update(
                        embedding_ms=(embedded - started) * 1000,
                        total_ms=(time.perf_counter() - started) * 1000,
                    )
                    samples.append(
                        dict(round=rnd, query_id=q["query_id"], mode=mode, path=path, **timing)
                    )
    summary = {}
    for mode in ["original", "minimal", "no_keepalive", "e2e"]:
        summary[mode] = {}
        for path in ["dense", "hybrid"]:
            subset = [s for s in samples if s["mode"] == mode and s["path"] == path]
            summary[mode][path] = {
                key: dict(
                    p50=percentile([s[key] for s in subset], 0.5),
                    p95=percentile([s[key] for s in subset], 0.95),
                )
                for key in ["client_ms", "server_ms", "embedding_ms", "total_ms"]
            }
            summary[mode][path]["n"] = len(subset)
    return dict(samples=samples, summary=summary)


def report(result):
    """Render the bounded findings without promoting development results to GO."""
    lines = [
        "# BM25 bounded development exploration",
        "",
        f"Execution: {result['execution_status']}; verdict: {result['verdict']}; type: EXPLORATORY.",
        f"Source SHA: `{result['source_sha']}`; plan hash: `{result['plan_sha256']}`.",
        "",
    ]
    if "error" in result:
        return "\n".join(lines + [result["error"]])
    lines += [
        "Native Qdrant 1.20 weights compress ranks: 1/((rank+1)/weight+59),",
        "rather than multiplying the rank score. An analytic fixture checks this.",
        "Separate single-leg requests can differ from native prefetch tie ordering;",
        "formula residuals are retained, not treated as exact prefetch traces.",
        "",
        "| Index / fusion | Answered queries | Dense Recall / nDCG | Hybrid Recall / nDCG | Recall W/T/L |",
        "|---|---:|---:|---:|---|",
    ]
    datasets = [
        ("baseline/native", result["quality"]["summary"]),
        ("baseline/posthoc-scalar", result["quality"]["posthoc_scalar_summary"]),
    ]
    if "wide_development" in result:
        datasets.append(("wide/native", result["wide_development"]["summary"]))
    for group, variants in datasets:
        for name, summary in variants.items():
            s = summary["all"]
            d, h = s["dense"], s["hybrid"]
            lines.append(
                f"| {group}/{name} | {s['answered_queries']} | {d['recall']:.4f} / {d['ndcg']:.4f} | {h['recall']:.4f} / {h['ndcg']:.4f} | {s['win_tie_loss']} |"
            )
    lines += [
        "",
        "Development has only 8 fact groups; no confidence-based GO is available.",
        "Scalar controls are post-hoc arithmetic on the captured development candidates,",
        "with deterministic ID tie-breaking, not a tested native runtime strategy.",
        "",
        "## Token matching probes",
        "",
        "| Index | Probe | Returned IDs |",
        "|---|---|---|",
    ]
    for index, rows in result["probes"].items():
        for row in rows:
            lines.append(f"| {index} | {row['probe']} | {row['ranked']} |")
    lines += [
        "",
        "Probe rescues: " + str(result["wide_probe_rescues"]),
        "A foil returned alongside the exact identifier remains an ambiguity;",
        "token matching alone does not establish exact-entity correctness.",
        "",
        "## Latency attribution",
        "",
        "| Mode/path | N | Client p50/p95 ms | Server p50/p95 ms | E2E/control total p50/p95 ms |",
        "|---|---:|---:|---:|---:|",
    ]
    for mode, paths in result["latency"]["summary"].items():
        for path, s in paths.items():
            c, v, t = s["client_ms"], s["server_ms"], s["total_ms"]
            lines.append(
                f"| {mode}/{path} | {s['n']} | {c['p50']:.2f}/{c['p95']:.2f} | {v['p50']:.2f}/{v['p95']:.2f} | {t['p50']:.2f}/{t['p95']:.2f} |"
            )
    lines += [
        "",
        "The no-keepalive total includes client construction/close overhead; client_ms excludes it.",
        "Minimal projection keeps the original dense24/hybrid8 response cardinalities.",
        "These 3x24-query microbenchmarks do not meet the original warm-p95 confirmation sample size.",
        "Client-minus-server includes encoding, transfer and other overhead, not only network time.",
        "No TCP mechanism is declared proven without transport traces.",
        "",
        "Recommendation: **" + result["recommendation"] + "**.",
        "No Runtime integration, no gate relaxation, and no new holdout was exposed in this run.",
        "",
    ]
    return "\n".join(lines)


def main():
    """Run bounded exploration and save raw evidence, never an experimental GO."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:16333")
    parser.add_argument("--ollama", default="http://127.0.0.1:11434")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = dict(
        execution_status="ERROR",
        experiment="EXPLORATORY",
        verdict="INCONCLUSIVE",
        config=CONFIG,
        source_sha=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        platform=platform.platform(),
        plan_sha256=digest(Path(__file__).with_name("PLAN.md")),
        script_sha256=digest(Path(__file__)),
        ci_run=os.getenv("GITHUB_RUN_ID", "local"),
        baseline_freeze_sha256=digest(
            Path(__file__).resolve().parents[1] / "independent" / "freeze.json"
        ),
    )
    server = None
    try:
        verify_freeze()
        memories, all_queries = load_dataset()
        queries = [q for q in all_queries if q["split"] == "dev"]
        result["probes"] = probe_tokens(args.server)
        embedding = Embedding(args.ollama)
        result["embedding"] = embedding.metadata
        server = DiagnosticServer(args.server, "bm25_ind_followup_" + uuid.uuid4().hex)
        result["qdrant"] = server.http.get("/").raise_for_status().json()
        assert result["qdrant"]["version"] == "1.20.0", "unexpected Server version"
        server.create_index(2560)
        for m in memories:
            server.upsert(m, embedding.embed(m["text"]))
            if m["deleted"]:
                server.delete(m["id"])
        vectors = {q["query_id"]: embedding.embed(q["query"]) for q in queries}
        result["quality"] = evaluate(server, queries, vectors)
        forbidden = {m["id"] for m in memories if m["deleted"]}
        for row in result["quality"]["rows"]:
            lists = [row["dense_candidates"], row["sparse_candidates"]] + [
                v["points"] for v in row["variants"].values()
            ]
            assert all(not forbidden.intersection(ids(points)) for points in lists), (
                "DELETED_ID_LEAK"
            )
        baseline = {r["probe"]: r for r in result["probes"]["baseline"]}
        rescued = [
            r["probe"]
            for r in result["probes"]["wide"]
            if r["probe"] + "/exact" in r["ranked"]
            and r["probe"] + "/exact" not in baseline[r["probe"]]["ranked"]
        ]
        result["wide_probe_rescues"] = rescued
        if rescued:
            wide = DiagnosticServer(args.server, "bm25_ind_wide_" + uuid.uuid4().hex)
            try:
                wide.create_index(2560, minimum=1, maximum=64)
                for m in memories:
                    # Real embeddings for both collections, never fixture vectors.
                    wide.upsert(m, embedding.embed(m["text"]))
                    if m["deleted"]:
                        wide.delete(m["id"])
                result["wide_development"] = evaluate(wide, queries, vectors)
            finally:
                wide.request("DELETE")
                wide.close()
        result["latency"] = latency(server, embedding, queries, vectors)
        result["execution_status"] = "SUCCESS"
        evidence = [s["all"]["win_tie_loss"][0] for s in result["quality"]["summary"].values()]
        if "wide_development" in result:
            evidence += [
                s["all"]["win_tie_loss"][0] for s in result["wide_development"]["summary"].values()
            ]
        result["recommendation"] = (
            "NEEDS_NEW_HOLDOUT_NOT_GO"
            if any(evidence)
            else "STOP_ENABLEMENT_NO_NEW_CORRECT_EVIDENCE"
        )
    except LookupError as error:
        result.update(execution_status="UNVERIFIED", error=str(error))
    except Exception as error:
        import traceback

        result.update(error=f"{type(error).__name__}: {error}", traceback=traceback.format_exc())
        if any(marker in str(error) for marker in ["CROSS_SCOPE", "DELETED_ID_LEAK"]):
            result["verdict"] = "NO-GO"
    finally:
        if server:
            try:
                server.request("DELETE")
            except Exception as error:
                result.update(execution_status="ERROR", error=f"cleanup failed: {error}")
            server.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        args.output.with_suffix(".md").write_text(report(result))
        print(result["execution_status"], result.get("error", result.get("recommendation")))
    return 0 if result["execution_status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
