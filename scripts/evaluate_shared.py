"""Reproduce private/shared latency and labeled quality on disposable real Qdrant."""

import argparse
import hashlib
import json
import math
import os
import platform
import selectors
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qdrant_memory.shared_client import call
from qdrant_memory.shared_service import SharedOwner, load_policy
from qdrant_memory.standalone import call_tool, open_runtime


def quality(rows, relevant, k=5):
    """Calculate binary-label Recall, Precision, MRR and normalized DCG."""
    found = [row["id"] in relevant for row in rows]
    dcg = sum(hit / math.log2(i + 2) for i, hit in enumerate(found[:k]))
    ideal = sum(1 / math.log2(i + 2) for i in range(min(k, len(relevant))))
    return {
        "recall_at_5": sum(found[:k]) / len(relevant),
        "precision_at_5": sum(found[:k]) / k,
        "mrr": next((1 / (i + 1) for i, hit in enumerate(found) if hit), 0),
        "ndcg_at_5": dcg / ideal,
    }


def timings(samples):
    """Report reproducible sample counts and milliseconds without hiding overhead."""
    values = sorted(samples)
    return {
        "samples": len(values),
        "p50_ms": statistics.median(values),
        "p95_ms": values[min(len(values) - 1, int(len(values) * 0.95))],
    }


def checked(result):
    """Never report a failed operation as a successful benchmark sample."""
    if "error" in result:
        raise RuntimeError(result["error"]["type"])
    return result


def _diagnostics(stream):
    """Return the bounded stderr tail of a failed helper process."""
    stream.seek(0)
    text = stream.read().strip()
    return text[-1500:] if text else "(no diagnostics)"


@contextmanager
def service(home, policy):
    """Measure readiness including process startup, validation and durable recovery."""
    started = time.perf_counter()
    # Diagnostics go to an unnamed file: the child can never stall on a full
    # stderr pipe, and a failed startup stays explainable.
    diagnostics = tempfile.TemporaryFile(mode="w+t")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "qdrant_memory.standalone",
            "--home",
            str(home),
            "shared-serve",
            "--policy",
            str(policy),
            "--port",
            "0",
        ],
        stdout=subprocess.PIPE,
        stderr=diagnostics,
        text=True,
        start_new_session=os.name != "nt",
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=120):
                raise TimeoutError(
                    "Shared service did not become ready: " + _diagnostics(diagnostics)
                )
        line = process.stdout.readline()
        ready = json.loads(line)
        if not ready.get("ready"):
            raise RuntimeError("Shared service startup failed: " + _diagnostics(diagnostics))
        startup_ms = (time.perf_counter() - started) * 1000
        rss = None
        if os.name != "nt":
            rss = int(
                subprocess.check_output(
                    ["ps", "-o", "rss=", "-p", str(process.pid)], text=True
                ).strip()
            )
        yield ready["url"], startup_ms, rss
    finally:
        if process.poll() is None:
            try:
                process.send_signal(signal.SIGINT if os.name != "nt" else signal.SIGTERM)
            except ProcessLookupError:
                pass  # The service exited between the poll and the signal.
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()
        diagnostics.close()


def run(args):
    """Use the same labeled corpus/pipeline for both modes; never open user profiles."""
    dataset = json.loads(args.dataset.read_text())
    token = uuid.uuid4().hex + uuid.uuid4().hex
    other = uuid.uuid4().hex + uuid.uuid4().hex
    reader_token = uuid.uuid4().hex + uuid.uuid4().hex
    collections = []
    with tempfile.TemporaryDirectory(prefix="qdrant-shared-evaluation-") as directory:
        root = Path(directory)
        policy = root / "policy.json"
        policy.write_text(
            json.dumps(
                {
                    "clients": [
                        {
                            "token_sha256": hashlib.sha256(credential.encode()).hexdigest(),
                            "principal": principal,
                            "owner_id": owner,
                            "space_id": "evaluation",
                            "visibility": "shared",
                            "permission": "write",
                        }
                        for credential, principal, owner in [
                            (token, "reader-writer", "evaluation"),
                            (other, "foreign", "foreign"),
                            (reader_token, "second-agent", "evaluation"),
                        ]
                    ]
                }
            )
        )
        policy.chmod(0o600)
        result = {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "model": args.model,
            "dimensions": args.dimensions,
            "dataset": args.dataset.name,
            "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
            "backend": "server" if args.qdrant_url else "embedded",
            "modes": {},
        }
        try:
            for mode in ("private", "shared"):
                home = root / mode
                home.mkdir()
                qdrant = {"collection": "shared_eval_" + uuid.uuid4().hex}
                if args.qdrant_url:
                    qdrant.update(mode="server", url=args.qdrant_url)
                    collections.append(qdrant["collection"])
                cfg = {
                    "scope": {"user_id": "evaluation", "agent_id": "evaluation"},
                    "qdrant": qdrant,
                    "embedding": {
                        "model": args.model,
                        "dimensions": args.dimensions,
                        "base_url": args.embedding_url,
                        "timeout_seconds": 120,
                    },
                    "search": {"top_k": 10, "candidate_k": max(24, len(dataset["memories"]))},
                }
                (home / "qdrant-memory.json").write_text(json.dumps(cfg))
                with tempfile.TemporaryFile(mode="w+t") as init_diagnostics:
                    completed = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "qdrant_memory.standalone",
                            "--home",
                            str(home),
                            "init",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=init_diagnostics,
                        timeout=180,
                    )
                    if completed.returncode != 0:
                        raise RuntimeError(
                            "Shared owner initialization failed: " + _diagnostics(init_diagnostics)
                        )
                if mode == "private":
                    with open_runtime(home) as runtime:

                        def private_invoke(name, data):
                            """Call the existing scoped tools on the open private Runtime."""
                            return call_tool(runtime, name, data)

                        result["embedding_fingerprint"] = runtime.store.embedder.fingerprint
                        result["modes"][mode] = measure(dataset, private_invoke, False)
                        result["modes"][mode].update(
                            repeat_load(
                                private_invoke, dataset["queries"][0]["query"], args.repeats
                            )
                        )
                else:
                    with service(home, policy) as (url, startup_ms, rss):

                        def invoke(name, data):
                            """Use a separate authenticated agent for every retrieval."""
                            credential = reader_token if name.endswith("search") else token
                            return checked(call(url, credential, name, data))

                        checked(
                            call(
                                url,
                                other,
                                "qdrant_memory_add",
                                {
                                    "text": "foreign private synthetic sentinel",
                                    "request_id": "sentinel",
                                },
                            )
                        )
                        measured = measure(dataset, invoke, True)
                        measured.update(startup_ms=startup_ms, owner_ready_rss_kib=rss)
                        # Scope check uses text, not relevance labels; every returned row must be authorized.
                        measured["scope_leakage_count"] = sum(
                            row["user_id"] != "evaluation"
                            for query in dataset["queries"]
                            for row in invoke("qdrant_memory_search", {"query": query["query"]})[
                                "memories"
                            ]
                        )
                        measured.update(
                            repeat_load(invoke, dataset["queries"][0]["query"], args.repeats)
                        )
                        result["modes"][mode] = measured
                    with service(home, policy) as (_, recovery_ms, _):
                        measured["restart_ready_ms_no_pending_backlog"] = recovery_ms
                    # Test-only interruption after durable plan admission, before any Qdrant call.
                    # Independent-process kill correctness is covered by the acceptance suite.
                    with open_runtime(home, shared_owner=True) as runtime:
                        owner = SharedOwner(runtime, load_policy(policy))

                        def interrupted(*_):
                            """Leave exactly one synthetic durable request for timed restart."""
                            raise InterruptedError("Synthetic benchmark interruption")

                        owner._apply = interrupted
                        try:
                            owner.call(
                                token,
                                "qdrant_memory_add",
                                {
                                    "text": "Recovery performance probe",
                                    "request_id": "recovery-probe",
                                },
                            )
                        except InterruptedError:
                            pass
                    with service(home, policy) as (url, recovery_ms, _):
                        restored = checked(
                            call(
                                url,
                                reader_token,
                                "qdrant_memory_search",
                                {"query": "Recovery performance probe"},
                            )
                        )
                        if not any(
                            row["text"] == "Recovery performance probe"
                            for row in restored["memories"]
                        ):
                            raise RuntimeError("Prepared request was not recovered")
                        measured["restart_ready_ms_one_pending_request"] = recovery_ms
            result["limitations"] = [
                "Synthetic pilot corpus, one local machine; not production performance or recall claims.",
                "Private mode calls an open Runtime; shared mode includes a fresh HTTP client and local hop per call.",
                "Four-reader latency includes Runtime serialization and local embedding contention.",
                "Restart times include Python startup and collection/embedding validation; one pending plan is measured separately.",
                "RSS is owner process only, excluding Ollama/Qdrant server, OS caches and children.",
                "Host application UIs and Cloud authentication are not benchmarked.",
            ]
            return result
        finally:
            if args.qdrant_url:
                from qdrant_client import QdrantClient

                with closing(QdrantClient(url=args.qdrant_url)) as client:
                    for collection in collections:
                        if client.collection_exists(collection):
                            client.delete_collection(collection)


def repeat_load(invoke, query, repeats):
    """Compare identical repeated queries under one and four readers in both modes."""

    def repeated(_):
        """Measure the full caller-visible search including lock queue wait."""
        began = time.perf_counter()
        checked(invoke("qdrant_memory_search", {"query": query}))
        return (time.perf_counter() - began) * 1000

    result = {}
    for readers, label in ((1, "repeated_single_reader"), (4, "four_readers")):
        began = time.perf_counter()
        if readers == 1:
            samples = [repeated(i) for i in range(repeats)]
        else:
            with ThreadPoolExecutor(max_workers=readers) as pool:
                samples = list(pool.map(repeated, range(repeats)))
        result[label] = {
            **timings(samples),
            "requests_per_second": repeats / (time.perf_counter() - began),
        }
    return result


def measure(dataset, invoke, shared):
    """Measure durable writes and cross-agent-equivalent retrieval against labels."""
    identifiers, writes, searches, samples = {}, [], [], []
    for i, memory in enumerate(dataset["memories"]):
        data = {"text": memory["text"]}
        if shared:
            data["request_id"] = f"save-{i}"
        began = time.perf_counter()
        saved = checked(invoke("qdrant_memory_add", data))
        writes.append((time.perf_counter() - began) * 1000)
        identifiers[memory["id"]] = saved["id"]
    for query in dataset["queries"]:
        began = time.perf_counter()
        rows = checked(invoke("qdrant_memory_search", {"query": query["query"]}))["memories"]
        searches.append((time.perf_counter() - began) * 1000)
        samples.append(quality(rows, {identifiers[key] for key in query["relevant_ids"]}))
    return {
        "writes": timings(writes),
        "write_records_per_second": len(writes) / (sum(writes) / 1000),
        "searches": timings(searches),
        "quality": {key: statistics.mean(row[key] for row in samples) for key in samples[0]},
    }


def main():
    """Print JSON evidence without credentials or memory contents."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "tests/fixtures/retrieval.json",
    )
    parser.add_argument("--model", default="qwen3-embedding:0.6b")
    parser.add_argument("--dimensions", type=int, default=1024)
    parser.add_argument("--embedding-url", default="http://127.0.0.1:11434")
    parser.add_argument("--qdrant-url")
    parser.add_argument("--repeats", type=int, default=24)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    print(json.dumps(run(args), indent=2))


if __name__ == "__main__":
    main()
