# Memory policy evaluation (2026-11-25)

This record measures how the shipped extraction prompt behaves on a labeled
agent-memory dataset, and how dense retrieval behaves across languages. It is a
baseline. It does not compare two prompt policies, because no candidate policy
was enabled.

## Assets

| Asset | Purpose | Size |
|---|---|---|
| `tests/quality/memory-policy-v1.json` | Admission cases across eight groups | 49 cases |
| `tests/fixtures/retrieval-multilingual.json` | Cross-lingual retrieval labels | 14 memories, 34 queries |
| `scripts/evaluate_lifecycle.py --dataset` | Admission runner with label validation and per-group metrics | Extended for labeled datasets |
| `scripts/evaluate.py --dataset` | Retrieval runner with per-category metrics | Reused without change |
| `tests/test_quality_policy_dataset.py` | Deterministic label, term and scoring checks | 9 tests |
| `tests/test_retrieval_multilingual_fixture.py` | Deterministic label and coverage checks | 4 tests |

Dataset identifiers: `agent-memory-policy-2026-11-25-v1`. Case digest
`8ec1767a9761bf0abac173c67d16b9d25049b1587f6619995abb12ac4c80d2f4`. Prompt
source digest `27754444f81ab1836d60ecfea7aa0e0188719a343e737f5fb98fd14c8d807402`.
Schema digest `899554d36c07fe2a187db77e73fae61cbadaff9c664ca4da7a4dfa1d13ae6814`.

## Environment

| Element | Value |
|---|---|
| Platform | macOS 27.2.1, arm64 |
| Python | 3.14.7 |
| Generation model | `qwen3:8b` through a local OpenAI-compatible endpoint |
| Embedding model | `qwen3-embedding:4b`, 2560 dimensions, Ollama |
| Embedding fingerprint | `8b5dffffa9f5477f8d0727a7b6b50e4b755e665a3ccb6ef0b500af46614a6b91` |
| Storage | Disposable embedded Qdrant |
| Credentials | None. Both runners refuse supplied API credentials |

## Commands

```bash
python scripts/evaluate_lifecycle.py \
  --dataset tests/quality/memory-policy-v1.json --output admission.json

python scripts/evaluate.py \
  --dataset tests/fixtures/retrieval-multilingual.json
```

## Admission baseline

Real local model. 49 cases, 0 provider failures, p50 8861 ms, p95 15633 ms.

| Metric | Value |
|---|---|
| Admission precision | 0.90 (27 correct rows of 30) |
| Admission recall | 0.93 (27 of 29 cases that should save a record) |
| False-memory rate | 0.15 (3 of 20 rejected cases produced a record) |
| Case success | 0.90 |

By group:

| Group | Cases | Precision | Recall | False-memory rate |
|---|---|---|---|---|
| A preferences | 7 | 0.80 | 1.00 | 0.33 |
| B durable facts | 7 | 1.00 | 1.00 | 0.00 |
| C engineering experience | 7 | 1.00 | 1.00 | 0.00 |
| D project knowledge | 6 | 0.80 | 1.00 | 0.50 |
| E decisions | 6 | 1.00 | 0.75 | 0.00 |
| F procedures | 6 | 0.80 | 1.00 | 0.50 |
| G security and privacy | 5 | 0.00 | 0.00 | 0.00 |
| H multilingual | 5 | 1.00 | 1.00 | 0.00 |

Three rejected statements became records:

- `a05` "For this task only, use bullet points." was stored verbatim. A
  task-scoped instruction became a durable preference.
- `d03` "The repository will probably move to Rust next year." was stored
  verbatim. A speculation became a project fact.
- `f02` "I ran the suite at 10:04 and it passed." was stored as an execution
  record. It is not a reusable procedure.

Two statements that should save a record did not:

- `e06` "The single-writer rule applies unless every writer is isolated." A
  conditional decision was dropped.
- `g04` "Do not send memory content to external telemetry." A negated durable
  constraint was dropped.

No security case produced a record. The four credential, quotation and
tool-output cases were rejected, so group G has a false-memory rate of 0.00. Its
recall is 0.00 because the one durable constraint in the group was missed.

The prompt was not changed. These are properties of the shipped prompt with this
model. The false memories share one shape: the model copies a statement that the
contract excludes. The misses share another shape: negation and condition.

## Retrieval baseline

Real local embeddings, disposable embedded Qdrant, top-k 10.

| Slice | Queries | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|---|
| All | 34 | 0.87 | 1.00 | 1.00 | 0.94 |
| English | 14 | 0.86 | 1.00 | 1.00 | 0.93 |
| Chinese | 14 | 0.86 | 1.00 | 1.00 | 0.93 |
| Mixed Chinese and English | 6 | 0.92 | 1.00 | 1.00 | 1.00 |

Search latency p50 53 ms, p95 86 ms. Every memory has one English query and one
Chinese query. A Chinese query that reaches a memory written in English therefore
shows cross-lingual recall without a translated copy. Mixed-language queries that
keep an exact identifier ranked the target first.

## Limitations

- Synthetic text and one 8B local model. Results do not transfer to another model
  or another embedding pipeline.
- The admission runner measures extraction only. It does not measure
  contradiction handling, replacement accuracy, cross-scope leakage or replay
  safety. Those stay in `scripts/evaluate_owner_lifecycle.py` and its datasets.
- Precision@5 is bounded by the mostly single-memory labels and is not comparable
  across datasets.
- No host-level behavioral acceptance ran in this cycle. These numbers do not
  show that agent behavior improves with the skill guidance.
- The labels follow the current extraction contract. A policy that admits agent
  lessons would need its own labels and its own baseline.

## Not run

- Real-model evaluation with hosted models or a hosted embedding service.
- Host-level cross-session acceptance on a desktop client.
- A candidate task-learning policy. It remains DESIGN ONLY.

## Reproduction notes

Both runners are offline. They refuse supplied API credentials and use a
disposable store. A different local model changes the admission numbers and must
be recorded under its own name.
