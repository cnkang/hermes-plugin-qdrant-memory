# Operations

Run `status` for local configuration, `doctor` for collection/pipeline/index
readiness, `stats` for exact memory count and ledger status counts, and `verify`
for payload/vector and latest migration integrity. A private schema identity point
is excluded from memory counts and recall. Embedded indexes are not created because
Qdrant local mode does not use payload indexes; server/Cloud create keyword,
datetime and integer indexes at provider/migration initialization.

Completed turns are persisted synchronously as small SQLite transactions, then
extraction and Qdrant work run on one context-preserving worker. Tool mutations
share the serialization lock. Background prefetch caches are session/scope-bound;
`prefetch()` returns cached results without embedding or network calls. Cache
generations prevent old background results from repopulating a switched session.

Events move through PENDING to COMMITTED or FAILED. Extraction results and prepared
operation payloads are persisted before remote mutations. Operation batches are
acknowledged only after Qdrant confirms completion. Timeouts, transport failures,
429 and 502/503/504 retry with bounded full-jitter backoff; auth/config/schema errors
fail immediately. FAILED rows remain visible and require an explicit `retry`.
Newer committed mutations mark older pending/failed writes to the same point
SUPERSEDED, preventing a later retry from reverting the new state.

Shutdown stops admission and waits up to the configured timeout. Remaining ledger
work survives process death. A live daemon finishing its drain owns and closes its
connections, avoiding teardown races. Never delete `state.db` to recover an error;
inspect stats and correct configuration before retrying.

Builtin replace/remove mirror the exact `metadata.previous_content`, scoped by
target and author. Older hosts without that authoritative value skip destructive
mirroring. Session end/compression provides supplementary extraction; every turn
already follows the durable write path. System prompt text and tool schemas remain
static through the conversation.

## Backup and restore

Stop the agent and every embedded maintenance client before copying state. Back up
`qdrant-memory.json`, the `qdrant-memory` state directory (including SQLite sidecar
files if present), and the embedded Qdrant path if it is configured elsewhere.
Keep credentials in a secret manager rather than a portable plaintext backup.

Restore the store and ledger from the same stopped snapshot into the intended
profile. Restore the matching embedding configuration, confirm directory/file
permissions, then run doctor, stats and verify before enabling the provider. A
ledger restored against unrelated target state may replay prepared changes; do
not mix arbitrary snapshots. Back up remote Qdrant through its deployment's own
snapshot procedure, coordinated with the stopped plugin ledger.

## Retry and maintenance ownership

```bash
hermes qdrant-memory stats
hermes qdrant-memory retry
hermes qdrant-memory verify
```

Failed raw events need the provider's trusted LLM context; retry only requeues them
for the next provider startup. Prepared operations can be committed by the CLI.
Pending operations are recovered automatically on startup; FAILED items require
operator correction and explicit retry. A missing migration operation invalidates
its manifest rather than counting it as completed. A fresh migration plan can repair
an unchanged target with stale open work using a new operation generation.

## Metrics

Stats reports destination-scoped ledger states and retry totals, plus bounded
samples of embedding/query/search/extraction latency, upsert throughput and cache
hit rate when recorded. Each metric retains at most 512 samples; reported p50/p95
are empirical sample percentiles, not a service-level guarantee. Cache-hit rate is
flushed on worker exit, so it may not appear while the current provider is active.

See [troubleshooting](troubleshooting.md) for mismatch, lock, missing-row and
credential failures, and [architecture](architecture.md) for persistence boundaries.
