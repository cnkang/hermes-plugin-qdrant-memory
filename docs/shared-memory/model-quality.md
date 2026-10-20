# Optional real-model lifecycle quality

This source-preview evaluation uses the existing Owner Runtime, actual Hermes
MemoryManager callbacks, actual Pi SDK sessions, and official HTTPS MCP clients.
It does not implement a second extraction or memory-write path. The Pi assistant
only acknowledges each turn deterministically; extraction and embeddings use real
already-installed local models (`qwen3:8b`, `qwen3-embedding:4b`) through Ollama on
loopback HTTP. No API key, Cloud service or model download is required.

The frozen anonymous corpus in `tests/quality/lifecycle-v1.json` contains 24 turns
across private Hermes, private Pi and an explicitly authorized project scope,
16 annotated positive facts, 8 negative turns and 32 retrieval queries. Concept
groups allow paraphrases without using another model as judge. Exact corpus and
model digests are recorded. This small synthetic corpus is a regression benchmark,
not an estimate of production accuracy or a user-interface acceptance test.

The gate requires precision >= 0.95, positive-case recall >= 0.90, Recall@5 >= 0.90,
MRR@5 >= 0.80, zero scope leaks and provider failures, safe replay, and persistence
across Owner restart. Missing observations fail the gate. After restart all 32
queries run again; IDs and every returned payload field must remain identical.
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
nonzero exit status; a failed evaluation must remain recorded as FAIL.
Observation/recording errors are separate from model-provider failures and also
fail the gate. Restart and receipt replay must not increase model calls. Model
discovery or setup failures still produce a redacted FAIL artifact.

Committed evidence:

- `owner-model-quality-evidence.json`: fingerprinted baseline before the evaluator
  review fixes; raw restart differences were limited to computed scores.
- `owner-model-quality-reviewed-evidence.json`: final run after the evaluator fixes,
  including explicit post-restart model-call and recording-error measurements.

`owner-model-quality.yml` is a manual lane for an explicitly provisioned,
disposable Linux test runner labelled `qdrant-memory-models`. It has no PR/fork
trigger and uses no production credentials. It checks out immutable Hermes and
MCP candidates and requires already-installed local models. Registering that
runner and executing the remote lane are separate operator tasks; defining the
workflow does not prove it ran. Scorer unit tests also run in ordinary test CI.
