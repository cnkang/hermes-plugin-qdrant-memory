# BM25 experiment archive

**Status: recorded for later reassessment; enablement work is on hold.**

Read the [complete English conclusion and durable evidence](results/bm25-2026-11-17/SUMMARY.md).
The fixed independent experiment is NO-GO. The bounded development follow-up
executed successfully but added no correct recall and did not surpass dense
ranking quality; it remains EXPLORATORY / INCONCLUSIVE. Keep dense as the default.

This branch and Draft PR #152 retain the code, frozen labels, plans, reports,
rankings, timing samples and immutable tested SHAs for future review. Recording
the experiment does not authorize Runtime integration or release.

| Location | Role |
|---|---|
| `bm25_experiment.py` and the original validation report | Historical slice-labelled exploratory baseline |
| `independent/` | Frozen per-question evaluation, fact-group split and full real-model latency comparison |
| `bm25_followup/` | Bounded development diagnostics for native/scalar weights, tokenization and latency attribution |
| `results/bm25-2026-11-17/` | Authoritative final record and curated durable evidence |

Reproduction commands and the requirements for reopening the research are in the
final record. Existing exposed holdouts cannot be reused as fresh confirmation.
