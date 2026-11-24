# Independent Hermes memory evaluation plan

Status: frozen before retrieval results; Codex reviews labels as authorized by the user.
This experiment is Server-only and does not change Runtime, MCP, Skills or defaults.

## Evidence and original gates

Historical run 43876725500, head 06038546450337a1c0e635649acf46894b36c267,
is exploratory: slice-wide shared relevant IDs do not establish per-question
validity. Mixed Recall@8 +0.10, Chinese +0.02, code unchanged, stale_fact
0.444 to 0.400. Its +1.4% is mixed mean latency, not warm p95.
The existing workflow's mixed +10pp, Chinese +10pp, code -2pp and mean
latency +20% displays remain historical checks. Chinese +10pp is not relaxed.
Fixture smoke/stability checks establish execution and lifecycle correctness,
not semantic quality. The new opt-in criterion below is independent of those
original checks and is preregistered before holdout execution.

## Dataset and review

`dataset.json` contains invented memories and individually authored questions.
The compact authoring format stores three distinct questions per fact group;
`dataset.py` expands these to explicit query_id, fact_group_id, tags, scope,
correct_ids, related_ids, conflict_ids and rationale. Correct IDs always mean
direct answers at the time requested, including historical questions. Related
evidence has no relevance gain; it is reported separately. No-answer questions
stay in denominators for their separate interference report, not Recall/nDCG.
Labels use local memory names qualified by fact group; point IDs are UUID5.
Every group carries its split. No group crosses development and holdout.

All labels are agent-authored and Codex-reviewed, not claimed human-reviewed.
The user explicitly delegated annotation verification to Codex. `review.json`
records the per-question audit and limitations. `freeze.json` locks dataset
bytes, configuration, review and this plan. Changes to labels
require a new dataset hash and a fresh review before any holdout execution.
The runner refuses unreviewed holdout. Development results remain exploratory.
No real Hermes memory, production collection or secret is used.

## Fixed comparison

Same temporary Qdrant Server collection, dense vectors, corpus, user AND agent
filter, candidate count 24, top 8, with payload, no score threshold for either
path. Dense returns the first 8 of 24. Hybrid uses the existing native BM25
multilingual index (min 2/max 15, lowercase true, enable_hnsw true), 24 sparse
candidates and Qdrant RRF k=60 with the scope filter on BOTH prefetches and
the final query. No tokenizer/candidate/RRF/threshold tuning after results.
Real evidence requires qwen3-embedding:4b; record installed model digest,
Ollama version, Qdrant version AND image digest, git SHA and freeze hash.
Missing model is UNVERIFIED, never a fixture substitution.

## Quality and uncertainty

Report per-tag query and independent fact-group counts, Recall@8, binary
nDCG@8 and recall wins/ties/losses. Use paired cluster bootstrap by fact group
(10,000 draws, seed 152, percentile 95% interval), retaining all questions in a
sampled group. Report representative regressions with exact ranked IDs.
For current stale_fact questions report conflict above ANY current answer,
including a conflict retrieved while a current answer is missing; also report
current-answer presence. Historical accuracy means a requested-time answer
ranked before every conflicting time-point answer. Multi-answer Recall uses
all direct evidence. No-answer interference is any returned item and returned
count, with no common dense/RRF score cutoff.

## Preregistered opt-in decision

This only decides whether a future Server-only, default-off experiment is
worth considering. It does not authorize implementing one in this PR.
GO requires reviewed, frozen, unexposed holdout; complete real-model evidence;
passing fixture lifecycle including restart; at least 30 mixed and 30 current
stale questions in at least 20 groups each, and at least 10 independent groups
in each Chinese/English/code/identifier stratum. Mixed Recall delta lower 95%
bound must be >0 and point gain >=0.05. Chinese/English/code/identifier Recall
and nDCG lower bounds must be >=-0.02. Stale-before-current delta upper bound
must be <=0.02; current presence and historical accuracy lower bounds >=-0.02.
No-answer interference delta upper bound <=0.02 (at least 10 groups).
Warm end-to-end p95 ratio upper 95% bound <=1.20 and point ratio <=1.20,
with >=3 rounds and >=100 pairs per round. Bootstrap latency by query group
across rounds, separately for each round and pooled; every round must meet
the budget. No single positive point estimate establishes GO.
Adequate evidence proving a boundary violation is NO-GO; overlapping bounds
or insufficient groups are INCONCLUSIVE. Cross-scope/deleted-ID leakage or
lifecycle failure is always NO-GO, independent of means or sample size.
Execution status (SUCCESS/ERROR/UNVERIFIED/BLOCKED_REVIEW) is separate.

## Latency and lifecycle

Separate quality from latency. Record first-call samples as cold-path (model
load may already have occurred during corpus embedding, so not process-cold).
Precompute vectors for retrieval-only timing. Warm both paths for two passes;
run three rounds, alternate dense/hybrid order by query and round. Each E2E
call embeds afresh. Save raw paired timings, p50/p95 nearest-rank milliseconds,
absolute and relative deltas. No cache replaces embedding in E2E timings.
Do not interpret local and CI machine latency as interchangeable.

Fixture lifecycle owns a separate temporary collection: add; replace same ID
with new text and vector; assert obsolete token no longer matches sparse;
assert new token does; delete; assert ID absent in dense/hybrid; verify foreign
user, foreign agent and null-agent separation. Restart the same container with
the same volume and repeat absence, updated-text and scope checks. Preserve
the updated ID separately from the deleted ID to test both after restart.

## Execution steps

- [ ] Validate data, freeze hashes and scoring tests locally.
- [ ] Run fixture lifecycle before and after temporary Server restart.
- [ ] Run real-model development quality and latency; publish provisional data.
- [ ] Codex review annotations as delegated by user and freeze review metadata.
- [ ] Run full holdout only after review; never silently tune on holdout.
- [ ] Push experiment branch, run full GitHub workflow and inspect exact-SHA
  logs/artifacts, including failure attribution, before updating the report.

If annotation review is pending, deliver the draft and working CI scaffold with
INCONCLUSIVE and do not claim independent validation or warm holdout p95.
