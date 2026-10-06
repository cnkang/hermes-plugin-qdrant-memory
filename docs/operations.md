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
