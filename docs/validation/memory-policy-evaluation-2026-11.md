# Memory policy evaluation (2026-11-26)

This record covers the evaluation harness, the labeled datasets, the measured
admission and retrieval baselines, and the Skill A/B run. It is synthetic,
local-model evidence. It is not a production quality claim.

## Harness changes in this revision

| Change | Before | Now |
|---|---|---|
| Latency percentile | `sorted(latencies)[-1]`, which is the maximum | Nearest-rank p50/p95 from `qdrant_memory.quality_metrics.latency_summary`. `max_ms` is reported separately |
| Execution status | One `status` value mixed health with quality | `execution.status` reports COMPLETE for a complete dataset run and FAIL for an incomplete run. The report includes `quality_gate: not_configured`, `dataset_valid`, and a completeness count |
| Provider failure | Counted as a correct rejection because the row list was empty | Reported as `provider_failures`. Quality rates are null for an incomplete run |
| Malformed rows | Counted as no record | Counted as `invalid_rows` and make the case incomplete. A valid empty memory array remains a clean rejection for a skip case |
| Duplicate matching rows | Inflated true rows | One matching row counts as true and the extra matching rows count as false. A duplicate result does not count as a successful save case |
| Negated and conditional records | A keyword hit passed | Optional `require_patterns` and `forbid_patterns`. The record must keep the negation or the condition |
| Semantic cases | Scored automatically | Flagged with `semantic_review` and counted in `review_required` |
| Label correction | Labels were frozen | v1 is preserved. Corrections live in v2 and in a held-out set. A `--rescore` mode re-scores stored model output without a new model call |

Rates return `null` when the denominator is zero, so an empty run cannot read as
quality 0 or quality 1.

## Datasets

| File | Purpose | Size |
|---|---|---|
| `tests/quality/memory-policy-v1.json` | Historical admission baseline, kept unchanged | 49 cases |
| `tests/quality/memory-policy-v2.json` | Development set with revised conditional and negated labels plus adversarial cases | 68 cases |
| `tests/quality/memory-policy-heldout-v1.json` | Held-out set authored before the candidate prompt existed | 24 cases |
| `tests/quality/prompts/extraction-candidate-v1.txt` | Isolated candidate instruction text, not shipped | 20 lines |
| `tests/fixtures/retrieval-multilingual.json` | Cross-lingual smoke-quality retrieval | 14 memories, 34 queries |
| `tests/fixtures/retrieval-scale-v1.json` | Hard-negative retrieval with near-duplicate project names | 360 memories, 72 queries |
| `tests/behavior/skill-ab-scenarios-v1.json` | Real-Hermes behavioral scenarios | 28 scenarios |

Deterministic checks: `tests/test_quality_policy_dataset.py`,
`tests/test_retrieval_multilingual_fixture.py` and
`tests/test_retrieval_scale_fixture.py`.

## Admission results

Local model `qwen3:8b`, temperature 0, no API credentials. Rates are null when a
denominator is zero.

| Dataset | Instruction | Precision | Recall | False-memory rate | Rows | Save / skip denominators |
|---|---|---|---|---|---|---|
| v1 | shipped | 0.90 | 0.93 | 0.15 | 30 | 29 / 20 |
| v2 (development, 0.7.3 reproduction) | shipped | 0.83 | 0.97 | 0.28 | 46 | 39 / 29 |
| v2 (development) | candidate-v1 | 0.70 | 0.92 | 0.39 | 50 | 38 / 28 |
| held-out | shipped | 0.72 | 0.93 | 0.50 | 18 | 14 / 10 |
| held-out | candidate-v1 | 0.93 | 1.00 | 0.10 | 15 | 14 / 10 |

The original shipped v2 table gave 23 rows, which cannot account for 38
successful save cases. The original per-case output was unavailable for this
audit. A fresh run against the released 0.7.3 source completed all 68 cases and
produced 38 true rows and 8 false rows. It recorded 38 of 39 successful saves
and 8 of 29 skips with a memory. The results above use the reproduction. Manual
review covered all four flagged cases. It confirmed the conditions in
`e06` and `e07`, the missing negated constraint in `g04`, and the preserved
negation in `g08`.

The historical candidate-v1 row comes from an incomplete run with two provider
failures. The old runner excluded those cases. The current evaluator retains
observed counts and reports null quality rates for runs with failed or invalid
outputs. The historical candidate metrics are descriptive only.

The two instruction sets disagree across datasets. The candidate is better on the
held-out set and worse on the development set. It misses the 0.95 precision and
0.05 false-memory targets, so it stays EXPERIMENTAL.

### What the shipped instruction keeps getting wrong

| Case | Dataset | Behaviour | Category |
|---|---|---|---|
| `a05` | v1, v2 | Stores "for this task only, use bullet points" | One-time instruction |
| `a09` | v2 | Stores an unsettled preference | Speculation |
| `b09` | v2 | Stores a hedged guess | Speculation |
| `d03`, `d08` | v1, v2 | Stores a future-architecture guess | Speculation |
| `f02`, `f08` | v1, v2 | Stores a one-time execution report | Execution status |
| `g07` | v2 | Admits "The deploy key is SYNTHETIC-ONLY-KEY-VALUE" | Credential shape |
| `h20` | held-out | Admits "My token is SYNTHETIC-HELDOUT-VALUE" | Credential shape |
| `g04` | v1, v2 | Misses a negated privacy constraint | Negation |
| `h09`, `h17` | held-out | Misses a cause-and-fix lesson and a conditional procedure | Evidence, condition |

Credential-shaped content was admitted in two independent datasets. That is the
most serious finding in this record. It supports the Skill's admission guidance
and argues against any automatic capture widening.

### Candidate instruction result

The candidate instruction keeps the same schema and the same Runtime. It changes
only the instruction text for an isolated experiment. On the development set it
produced more rows and more false memories. On the held-out set it produced
fewer and cleaner rows. Because the result is inconsistent and misses two
targets, the candidate stays EXPERIMENTAL and no production prompt changes in
this revision.

## Retrieval results

Same dense pipeline, `qwen3-embedding:4b`, 2560 dimensions, disposable embedded
Qdrant, top-k 10.

| Dataset | Memories | Labeled queries | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|---|---|
| multilingual | 14 | 34 | 0.87 | 1.00 | 1.00 | 0.94 |
| scale | 360 | 68 | 0.87 | 0.97 | 0.99 | 0.92 |

Scale fixture by slice:

| Slice | Queries | Recall@1 | Recall@5 | MRR |
|---|---|---|---|---|
| English | 21 | 0.86 | 1.00 | 0.91 |
| Chinese | 23 | 0.96 | 1.00 | 0.97 |
| Mixed | 22 | 0.86 | 0.95 | 0.91 |
| Multi-relevant | 2 | 0.13 | 0.63 | 0.75 |

Search latency p50 53 ms, p95 79 ms. Recall@5 falls from 1.00 on 14 memories to
0.97 on 360 memories with near-duplicate project names, which is the realistic
direction. Multi-relevant Recall@1 is capped by four labeled targets per query.
Recall@5 is the meaningful figure there.

### Query construction

Ten information needs, each written twice over the same 360-memory corpus.
Neither variant contains the gold record text, and the broad variant never names
the project.

| Variant | Queries | Recall@1 | Recall@5 | MRR |
|---|---|---|---|---|
| Broad ("What did we decide about the database?") | 10 | 0.00 | 0.10 | 0.06 |
| Focused ("Project Cedar primary database choice") | 10 | 0.80 | 1.00 | 0.90 |

Search latency stayed the same (p50 53 ms for both). The result supports the
first query rule in the Skill: name the subject and keep the identifier. It does
not test decomposition or multilingual rewriting.

Four abstention queries have no relevant memory. Their top-1 scores are p50 0.64
and max 0.80. They are recorded as observations only: the Runtime has no
calibrated abstention threshold, and this record does not invent one.

## Skill A/B behavioral run

| Metric | Skill 0.4.8 | Skill 0.4.9 | Delta |
|---|---|---|---|
| Scenarios run (per arm) | 28 | 28 | — |
| Skill loaded (`skill_view`) | 4 | 9 | +5 |
| Passed declared expectations | 13 | 13 | 0 |
| Failed declared expectations | 6 | 8 | +2 |
| Runs without a final answer in 8 turns | 5 | 3 | -2 |
| Manual review required | 4 | 4 | 0 |
| Memory searches (total) | 15 | 19 | +4 |
| Qdrant writes (total) | 0 | 0 | 0 |
| Tokens (total) | 2,470,103 | 2,219,044 | -10% |
| Wall time (total) | 1,475 s | 1,574 s | +7% |

Host and model: local Hermes profile per arm, explicit stdio MCP tools only and
no native lifecycle. The model is the `opencode-go` free router `hungerg-free`.
Each scenario used one fresh session, an empty working directory and at most 8
turns. Both arms used the
same 14 seeded memories, the same tool surface and the same prompts.

Scoring: a scenario passes only when its declared expectation holds in the tool
trace and the final text. The agent's own claim is never treated as evidence.

Reading the result:

- The rewritten description more than doubled the selection rate (4 to 9 of 28).
- Task outcomes did not improve: both arms passed 13 scenarios. The candidate
  traded two incomplete runs for two extra failures.
- The candidate searched more often, including one run that looped to the
  8-turn limit with 8 searches. That is the over-search risk the guidance warns
  about.
- Neither arm wrote to Qdrant. In the two explicit-save scenarios both arms used
  the host's built-in memory tool instead of `qdrant_memory_add`, so the scenario
  failed. The Skill cannot choose the host's tool preference.
- Several failures are model-capability limits, not Skill defects: the free
  router model hallucinated a database answer from a web page in one run and
  produced no final text in others.
- In this harness the host deferred the MCP tools behind its tool search, and the
  Skill body reached the model only when `skill_view` ran (4 of 28 baseline runs
  and 9 of 28 candidate runs). The comparison therefore measures
  description-driven selection more than the body guidance.

This table is behavioral evidence for one host and one model. It is not a user
experience guarantee, and it does not cover native lifecycle capture.

## Limitations

- Admission used one local extraction model. The Hermes A/B used one hosted
  free router model.
- The admission runner measures extraction only. Contradiction handling,
  replacement accuracy, cross-scope leakage and replay safety remain in
  `scripts/evaluate_owner_lifecycle.py`.
- Semantic review cases are flagged, not auto-judged. Four v2 cases and five
  held-out cases need a human read.
- The A/B used one repetition per scenario. It shows direction, not a stable
  effect size.
- The query-construction comparison covers a named subject against a broad
  question. It does not cover decomposition or multilingual rewriting.
- No Pi or Codex behavioral run.

## Not run

- Admission with a hosted extraction model or retrieval with a hosted embedding
  service.
- Native-lifecycle A/B (prefetch and extraction) on Hermes.
- A task-learning candidate. It remains DESIGN ONLY.

## Reproduction

```bash
python scripts/evaluate_lifecycle.py --dataset tests/quality/memory-policy-v2.json
python scripts/evaluate_lifecycle.py --dataset tests/quality/memory-policy-heldout-v1.json \
  --instructions tests/quality/prompts/extraction-candidate-v1.txt
python scripts/evaluate_lifecycle.py --dataset tests/quality/memory-policy-heldout-v1.json \
  --rescore stored-report.json --output rescored.json
python scripts/evaluate.py --dataset tests/fixtures/retrieval-scale-v1.json
python scripts/behavior_ab.py --profile <hermes-profile> --cli <qdrant-memory> \
  --scenarios tests/behavior/skill-ab-scenarios-v1.json --out report.json --run \
  --model <model> --provider <provider>
```

These runs use synthetic text. The admission commands use the default local
endpoint. The behavioral runner uses the model provider configured in its Hermes
profile. Use an isolated Hermes profile and a disposable qdrant-memory profile
for that run.

Admission re-scoring requires one stored result per dataset case ID. The
input digest binds each ID to the user and assistant text. Label edits can
reuse saved model output. Legacy reports without a verifiable digest are
marked `input_verified: false`. A failed or invalid case leaves the quality
rates null. Re-run a legacy report if it counted invalid rows without storing
them.
Behavior re-scoring requires the same scenario IDs. It marks a scenario
incomplete when its saved trace lacks exit codes or the evidence needed for an
exact-ID check. The historical A/B counts above predate those trace fields.
