Local run used uncommitted follow-up sources on parent 05f0777. Later changes only add execution metadata and failure attribution; retrieval settings and traces are unchanged.

# BM25 bounded development exploration: local run

Execution: SUCCESS; verdict: INCONCLUSIVE; type: EXPLORATORY.
Source SHA: `05f077702ea58ad10e05039744e12c993511df6b`; plan hash: `30621d719afd4d6c5b0d3eb0053993cd7d613cfe996070a3904309a88c3983f9`.

Native Qdrant 1.20 weights compress ranks: 1/((rank+1)/weight+59),
rather than multiplying the rank score. An analytic fixture checks this.
Separate single-leg requests can differ from native prefetch tie ordering;
formula residuals are retained, not treated as exact prefetch traces.

| Index / fusion | Answered queries | Dense Recall / nDCG | Hybrid Recall / nDCG | Recall W/T/L |
|---|---:|---:|---:|---|
| baseline/native/rrf_1_1 | 20 | 1.0000 / 0.7789 | 1.0000 / 0.6860 | [0, 20, 0] |
| baseline/native/rrf_2_1 | 20 | 1.0000 / 0.7789 | 1.0000 / 0.6945 | [0, 20, 0] |
| baseline/native/rrf_4_1 | 20 | 1.0000 / 0.7789 | 1.0000 / 0.6799 | [0, 20, 0] |
| baseline/posthoc-scalar/rrf_1_1 | 20 | 1.0000 / 0.7789 | 1.0000 / 0.7019 | [0, 20, 0] |
| baseline/posthoc-scalar/rrf_2_1 | 20 | 1.0000 / 0.7789 | 1.0000 / 0.7213 | [0, 20, 0] |
| baseline/posthoc-scalar/rrf_4_1 | 20 | 1.0000 / 0.7789 | 1.0000 / 0.7495 | [0, 20, 0] |
| wide/native/rrf_1_1 | 20 | 1.0000 / 0.7789 | 1.0000 / 0.7045 | [0, 20, 0] |
| wide/native/rrf_2_1 | 20 | 1.0000 / 0.7789 | 1.0000 / 0.7271 | [0, 20, 0] |
| wide/native/rrf_4_1 | 20 | 1.0000 / 0.7789 | 1.0000 / 0.7137 | [0, 20, 0] |

Development has only 8 fact groups; no confidence-based GO is available.
Scalar controls are post-hoc arithmetic on the captured development candidates,
with deterministic ID tie-breaking, not a tested native runtime strategy.

## Token matching probes

| Index | Probe | Returned IDs |
|---|---|---|
| baseline | model | ['model/exact', 'model/foil'] |
| baseline | pr | ['pr/foil', 'pr/exact'] |
| baseline | key | ['key/exact', 'key/foil'] |
| baseline | score | ['score/exact', 'score/foil'] |
| baseline | short | [] |
| wide | model | ['model/exact', 'model/foil'] |
| wide | pr | ['pr/exact', 'pr/foil'] |
| wide | key | ['key/exact', 'key/foil'] |
| wide | score | ['score/exact', 'score/foil'] |
| wide | short | ['short/exact'] |
| code | model | ['model/exact', 'model/foil'] |
| code | pr | ['pr/exact', 'pr/foil'] |
| code | key | ['key/exact', 'key/foil'] |
| code | score | ['score/exact', 'score/foil'] |
| code | short | ['short/exact'] |

Probe rescues: ['short']
A foil returned alongside the exact identifier remains an ambiguity;
token matching alone does not establish exact-entity correctness.

## Latency attribution

| Mode/path | N | Client p50/p95 ms | Server p50/p95 ms | E2E/control total p50/p95 ms |
|---|---:|---:|---:|---:|
| original/dense | 72 | 47.66/56.10 | 0.90/2.09 | 47.70/56.13 |
| original/hybrid | 72 | 48.34/55.43 | 1.26/2.71 | 48.46/55.46 |
| minimal/dense | 72 | 48.94/54.09 | 1.30/2.36 | 48.96/54.17 |
| minimal/hybrid | 72 | 49.99/55.77 | 1.67/3.35 | 50.03/55.89 |
| no_keepalive/dense | 72 | 5.86/12.54 | 0.83/1.84 | 12.14/21.93 |
| no_keepalive/hybrid | 72 | 6.58/11.87 | 1.12/2.14 | 12.92/20.77 |
| e2e/dense | 72 | 7.13/14.81 | 1.27/2.35 | 63.63/94.57 |
| e2e/hybrid | 72 | 7.99/13.24 | 1.79/3.60 | 64.25/98.38 |

The no-keepalive total includes client construction/close overhead; client_ms excludes it.
Minimal projection keeps the original dense24/hybrid8 response cardinalities.
These 3x24-query microbenchmarks do not meet the original warm-p95 confirmation sample size.
Client-minus-server includes encoding, transfer and other overhead, not only network time.
No TCP mechanism is declared proven without transport traces.

Recommendation: **STOP_ENABLEMENT_NO_NEW_CORRECT_EVIDENCE**.
No Runtime integration, no gate relaxation, and no new holdout was exposed in this run.
