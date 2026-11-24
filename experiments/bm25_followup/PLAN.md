# Bounded BM25 follow-up, preregistered before execution

Exploratory only. Original frozen evaluator, labels, gates and Runtime stay intact.
Compare dense, fixed 1:1 RRF, 2:1 RRF and 4:1 RRF on the existing development
queries. Keep k=60, candidates=24, top8 and model/image digests fixed. Capture
full dense and sparse top24, fused top24, direct-answer positions and candidate
union recall. Validate native weighted RRF rather than assuming API support.

Probe multilingual 2..15, multilingual 1..64 and code 1..64 using deterministic
fixture vectors in separate collections. Record matching/ranking for similar
identifiers and short tokens. These are tokenizer diagnostics, not real-model
quality evidence. If wide multilingual rescues a missing exact identifier,
test that one text-index variant with otherwise fixed 2:1 fusion on development.
No other parameter grid or BM25 k/b tuning in this round.

Development diagnostic amendment before CI: source inspection of pinned Qdrant
1.20.0 shows native weights compress rank positions, not multiply RRF scores.
Validate this with an analytic fixture. Add offline scalar 2:1/4:1 arithmetic
controls using captured development candidates to distinguish the two semantics.
These controls were added after seeing the first local development run, are
post-hoc exploratory, and cannot establish a selected configuration or GO.

Latency: original payloads versus a common scope/ID-only payload projection;
record client total, HTTP response, Qdrant response time, JSON decode, embedding
and E2E separately. Warm paths twice, alternate order for three rounds of 24
development query pairs per comparison. Preserve raw samples. This is a diagnostic
microbenchmark, below the original >=100 pairs/round confirmation requirement.
Run real embeddings for E2E; never substitute fixture vectors for semantic data.
Check the no-keepalive transport control separately, not as a selected improvement.

Do not select using the exposed holdout. An optional old-holdout regression pass
uses only the already specified weights and must be labelled exploratory. No GO
may be assigned to this follow-up. Review development first: if variants merely
recover dense performance without new correct evidence, stop recommending enablement.
If a variant adds correct evidence without quality/temporal regressions, freeze a
separate plan and new fact-group holdout before confirmation. Ranking-only changes
are reported, but do not establish the missing recall benefit or waive original gates.

Run locally against an owned temporary server, then publish reproducible exploratory
CI and reports on the existing Draft branch. Keep raw evidence and exact source SHA.
