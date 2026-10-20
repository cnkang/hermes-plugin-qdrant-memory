# Optional real-model lifecycle quality

This source-preview evaluation uses the existing Owner Runtime, actual Hermes
MemoryManager callbacks, actual Pi SDK sessions, and official HTTPS MCP clients.
It does not implement a second extraction or memory-write path. The Pi assistant
only acknowledges each turn deterministically. Extraction and embeddings use real
already-installed local models (`qwen3:8b`, `qwen3-embedding:4b`) through Ollama on
loopback HTTP. No API key, Cloud service or model download is required.

The frozen anonymous corpus is in `tests/quality/lifecycle-v1.json`. It contains
24 turns across private Hermes, private Pi and an explicitly authorized project
scope. It also contains 16 annotated positive facts, 8 negative turns and 32
retrieval queries. Concept
groups allow paraphrases without using another model as judge. Exact corpus and
model digests are recorded. This small synthetic corpus is a regression benchmark,
not an estimate of production accuracy or a user-interface acceptance test.

The gate requires precision >= 0.95, positive-case recall >= 0.90, Recall@5 >= 0.90
and MRR@5 >= 0.80. It also requires zero scope leaks, zero provider failures, safe
replay and persistence across Owner restart. Missing observations fail the gate. After restart all 32
queries run again. IDs and every returned payload field must remain identical.
Computed similarity scores are excluded from payload comparison, but ranking is
scored again and raw comparison diagnostics remain in the report. Event receipts
must be unchanged and replay must not call the model again. Retrieval and model
latency summaries use measured p50 and nearest-rank p95.

From this checkout, with Python 3.11, `.[remote]`, Hermes test dependencies and
the MCP checkout's locked Pi dependencies installed:

```sh
PYTHONPATH="$PWD:/path/to/hermes-agent" python scripts/evaluate_owner_lifecycle.py \
  --adapter-root /path/to/qdrant-memory-mcp --output /tmp/owner-quality-new.json
```

The output path must be new. Each run uses a private temporary home, synthetic
credentials and temporary Embedded Qdrant data, then closes and removes them.
Reports contain metrics and hashes, not tokens or memory text. Failures return a
nonzero exit status. A failed evaluation must remain recorded as FAIL.
Observation/recording errors are separate from model-provider failures and also
fail the gate. Restart and receipt replay must not increase model calls. Model
discovery or setup failures still produce a redacted FAIL artifact.

Committed evidence:

- `owner-model-quality-evidence.json`: fingerprinted baseline before the evaluator
  review fixes. Raw restart differences were limited to computed scores.
- `owner-model-quality-reviewed-evidence.json`: earlier run after the evaluator fixes,
  including explicit post-restart model-call and recording-error measurements.
- `owner-model-quality-drained-evidence.json`: current baseline PASS after awaiting
  the public Host admission barrier before the Provider idle barrier. All 24
  captures and 32 queries, replay and restart checks ran with real local models.
- `owner-mutation-initial-failure.json`: first full v1 + mutation run before the
  replacement-intent, Host-admission barrier and denial-probe fixes. It records
  FAIL and predates the `baseline_executed`/`evaluation_mode` fields; it is not
  passing evidence for the current candidate.
- `owner-mutation-quality-evidence.json`: historical full run whose cross-Agent
  handoff claim is withdrawn: both read and write credentials belonged to `pi`.
  Its raw PASS is retained for traceability, not current acceptance. The revised
  scorer requires distinct logical-Agent identities from the stored provenance
  and the actual reader grant; missing or equal identities fail closed.

The `owner-model-quality.yml` workflow uses a disposable Linux test runner with the `qdrant-memory-models` label.
An operator must provision this runner explicitly.
The workflow has no pull-request or fork trigger and uses no production credentials.
It checks out immutable Hermes and MCP candidates and requires installed local models.
Workflow configuration alone does not prove execution.
Runner registration and remote execution require separate operator actions.

Ordinary test CI also runs the scorer unit tests.

Both lanes require Linux x86_64 and Python 3.11.
They install the hashed wheels in `requirements-quality.txt`.
The local Runtime build uses the pinned build backend and an offline dependency check.
The installer keeps normal dependency resolution and runs `pip check`.
The hosted driver job verifies this environment without loading models.

To update the snapshot, run this command from the Runtime checkout:

```sh
uv pip compile pyproject.toml requirements-quality.in --extra remote \
  --constraint requirements.txt --python-version 3.11 \
  --python-platform x86_64-unknown-linux-gnu --generate-hashes \
  --no-header --no-annotate --output-file requirements-quality.txt
```

Review the resulting versions and hashes before you use the new snapshot.

## Driver contract

`pi-quality-driver.yml` defines a contract job on a standard GitHub-hosted runner.
The job uses the fixed Pi SDK and actual extension with a synthetic Owner transport.
It repeats prompts and delays delivery across cases.
Each receipt must keep its original event-to-case association.

The job rejects skipped or absent tests.
This workflow never selects the model runner.
Its result needs a completed CI run.

## Mutation and multilingual extension

Pass `--scenarios tests/quality/lifecycle-mutations-v1.json` to additionally run
the frozen mutation corpus after the v1 capture/retrieval/restart gate. It uses
Chinese Hermes turns in separate sessions to measure explicit supersession and
same-logical-Agent continuation from a second device. Actual Pi hooks capture a
Chinese documentation preference and an authorized project architecture decision.

All explicit deletion, replay and concurrent CAS operations go through verified
HTTPS MCP. Two independently authenticated logical Agents race one project-memory
version: exactly one update and one conflict are required. A project read grant
must be unable to delete, then must fail authentication after revocation. Another
restart verifies durable deletion, winning receipts, revocation, retained facts
and zero additional model calls. Generic network failure cannot count as denial.

The extension fails if any scenario is missing/false or an old superseded fact
remains in the measured latest-fact search. `obsolete_hit_rate` describes only
that annotated query, not a production hallucination or overall injection rate.
The v1 and mutation corpora retain separate versions and hashes; v1 evidence
does not prove the optional extension ran. The manual model workflow explicitly
enables this extension. Its actual-run evidence must be recorded separately.

For focused development, `--scenarios-only` executes only the mutation corpus.
Its report explicitly states `evaluation_mode=mutations-only`, zero baseline
cases/queries and `baseline_executed=false`. A diagnostic PASS does not certify
the v1 baseline. The manual workflow requires both baseline and mutation PASS.

Explicit supersession uses a `replacement_intent` extraction flag. It requests
relation review of up to five existing candidates in the same authorized scope,
including below the ordinary similarity cutoff. The original user statement is
provided as untrusted evidence; only factual `SUPERSEDES` can authorize choosing
an update instead of an add. The flag never changes the grant, target scope or
write permissions. New extraction schemas require every property, following the
[strict Structured Outputs contract](https://developers.openai.com/api/docs/guides/structured-outputs).
Missing/false flags in legacy durable candidates retain the old review policy.
Embeddings and stored payload schema stay
unchanged. This path can add up to five relation-model calls for an explicitly
replacing fact; measured latency must be reported rather than hidden.
