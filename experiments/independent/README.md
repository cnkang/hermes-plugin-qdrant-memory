# Independent BM25 experiment

132 individually labelled questions, 44 independent fictional fact groups,
172 memories. Development: 24 questions / 8 groups. Holdout: 108 questions /
36 groups. Holdout mixed: 54 questions / 36 groups; current stale_fact: 30
questions / 30 groups. Tags overlap. `dataset.py` reports all exact counts.
Annotation verification was delegated to Codex by the user. See REVIEW.md;
this is not claimed to be independent human annotation.

`annotations.jsonl` is the expanded, reviewable per-question contract.
`dataset.json` is its compact authoring source. `freeze.json` locks both,
the annotation review, experiment plan, retrieval configuration and scorer.
Holdout results are independent of the legacy script and its slice-wide IDs.
The old script and its results remain exploratory baseline evidence.

## Reproduce

Use an isolated server and fresh volume, never a production endpoint. The
runner only creates new `bm25_ind_*` collections and refuses other names.
Only `httpx==0.28.1` is required; these modules import no Runtime/MCP/Skills.

```sh
python -m venv /tmp/bm25-eval-venv
/tmp/bm25-eval-venv/bin/pip install httpx==0.28.1
export QDRANT_IMAGE_DIGEST=sha256:9bfc89f9914fc14876a88c05270685921a55b8877b254a98bcc1cc2f0a424cd2
docker run -d --name bm25-independent-local -p 127.0.0.1:16333:6333 \
  -v bm25_independent_local:/qdrant/storage \
  qdrant/qdrant@"$QDRANT_IMAGE_DIGEST"
ollama pull qwen3-embedding:4b
/tmp/bm25-eval-venv/bin/python -m unittest discover -s experiments/independent -p 'test_*.py' -v
/tmp/bm25-eval-venv/bin/python experiments/independent/lifecycle.py \
  --container bm25-independent-local --output /tmp/bm25-results/lifecycle.json
/tmp/bm25-eval-venv/bin/python experiments/independent/run.py --split dev \
  --rounds 1 --latency-pairs 2 --latency-queries 2 \
  --lifecycle-report /tmp/bm25-results/lifecycle.json --output /tmp/bm25-results/dev.json
/tmp/bm25-eval-venv/bin/python experiments/independent/run.py --split holdout \
  --rounds 3 --latency-pairs 100 --lifecycle-report /tmp/bm25-results/lifecycle.json \
  --output /tmp/bm25-results/holdout.json
docker rm -f bm25-independent-local
```

Start Ollama on loopback port 11434 before these commands. Both the model name
and model digest are checked. Model or digest unavailable means UNVERIFIED;
fixture vectors are never used for quality or latency. Qdrant is 1.20.0 with a
pinned multi-platform image digest. Lifecycle checks verify the actual container
image and unchanged storage mount across restart.

Pushes changing this directory on `codex/bm25-hybrid-eval` run
`.github/workflows/bm25-independent.yml`; workflow_dispatch can also run the
same frozen configuration. The long job has a 180-minute limit. It first runs
scoring tests and deterministic lifecycle, then real development and the full
holdout plus three latency rounds. Artifacts include raw paired timings, exact
rankings, model/image metadata, SHA, expanded annotations and two separate
statuses. A green Actions result means successful execution, even if the
experiment verdict is NO-GO or INCONCLUSIVE. Missing model, dataset errors,
exceptions and failed lifecycle assertions fail execution and retain status
artifacts. Scope/deletion leakage cannot produce GO.

The holdout has only two no-answer groups; ten are required by the opt-in plan.
Do not infer GO from the point estimates alone. A failed gain or noninferiority
boundary can establish NO-GO despite this separate sample-size limitation.
No settings or labels may be tuned against this exposed holdout. Any future
tokenizer/RRF/candidate/threshold variant needs new independent groups.
