# Bounded BM25 development exploration

This follow-up does not edit the frozen independent evaluator or reinterpret its
holdout as new confirmation. Read PLAN.md for the bounded candidates and the
post-hoc distinction between native and scalar RRF weighting.

Use the same pinned temporary Qdrant image and real model setup documented in
`../independent/README.md`. Then run:

```sh
python -m unittest discover -s experiments/independent -p 'test_*.py' -v
python experiments/independent/lifecycle.py --container bm25-followup-local --output /tmp/bm25-followup/lifecycle.json
python experiments/bm25_followup/explore.py --output /tmp/bm25-followup/results.json
```

The CLI assumes port 16333 for the temporary Server and 11434 for Ollama;
`--server` and `--ollama` override these. Container names passed to lifecycle
must refer to the disposable pinned server created for this experiment.
No production collection is accessed. JSON and sibling Markdown contain native
candidate/fusion traces, token probes, source/plan hashes and raw timing samples.
Local execution before commit has a parent source SHA; script hashes identify
the actual files. CI records the exact committed source SHA and run ID.

`bm25-followup.yml` runs this development exploration, existing scoring checks
and fixture lifecycle, and uploads artifacts even on failure. Execution success
is distinct from the recommendation. No output can establish GO: the dataset
is development data, the latency sample is diagnostic, and scalar controls were
added after the first local development result. New independent evidence is
required before proposing any future Server-only default-off entry point.

Native weighted RRF semantics were verified against Qdrant v1.20.0 source:
https://github.com/qdrant/qdrant/blob/v1.20.0/lib/segment/src/common/reciprocal_rank_fusion.rs
The formula compresses rank positions, rather than multiplying each score.
The fixture checks a rank-zero dense-only contribution with weight two.
Reconstructing native fusion from separate leg requests is approximate when tie
ordering or underlying candidate selection differs; retain formula residuals.
