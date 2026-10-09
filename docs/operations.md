# Operations

Run `status` for local configuration. Run `doctor` for collection, pipeline and
index readiness. Run `stats` for the exact memory count and ledger status counts.
Run `verify` for payload, vector and latest migration integrity. A private schema
identity point stays out of memory counts and recall. The plugin does not create
embedded indexes because Qdrant local mode does not use payload indexes.
Server/Cloud create keyword, datetime and integer indexes at provider or
migration initialization.

Hermes submits the completed-turn `sync_turn` callback through an in-memory host
background queue. Once the callback reaches the provider, the plugin commits its
event synchronously in a small SQLite transaction. Extraction and Qdrant work then
run on one context-preserving plugin worker. A process exit or bounded host
shutdown can lose a callback still waiting in the host queue. The plugin's durable
recovery starts at its own SQLite admission point. Tool mutations share the
serialization lock. Background prefetch caches belong to a session and scope.
`prefetch()` returns cached results without embedding or network calls. Cache
generations prevent old background results from repopulating a switched session.

Events move through PENDING to COMMITTED or FAILED. The plugin persists extraction
results and prepared operation payloads before remote mutations. It acknowledges
operation batches only after Qdrant confirms completion. Timeouts, transport
failures, 429 and 502/503/504 retry with bounded full-jitter backoff. Auth,
config and schema errors fail immediately. FAILED rows remain visible and require
an explicit `retry`. Newer committed mutations mark older pending/failed writes
to the same point SUPERSEDED, preventing a later retry from reverting the new
state.

Shutdown stops admission and waits up to the configured timeout. At the deadline,
the plugin discards queued jobs, and durable work remains available for recovery.
An in-flight network call cannot be forcibly cancelled. Timeout raises an explicit
error, and the worker retains its writer lease and connections until it exits.
The plugin refuses a replacement provider or a mutating maintenance CLI for that
profile destination while the old worker owns the lease. Lease files live under
`HERMES_HOME`, so separate profile homes on the same machine do not coordinate
writes to one remote collection. Deploy only one writer per destination across
all profiles and hosts. Leases do not provide distributed exclusion or make a
shared absolute storage path safe. Stop writers on every host before maintenance.
Never delete `state.db` to recover an error. Inspect stats and correct
configuration before retrying.

Builtin replace/remove mirror the exact `metadata.previous_content`, scoped by
target and author. Older hosts without that authoritative value skip destructive
mirroring. Session end/compression provides supplementary extraction. Every turn
whose `sync_turn` callback reaches the provider follows the plugin's durable write
path. Checkpoint API v2 synchronously commits
filtered user/assistant evidence before compression; disk failures propagate and
prevent acknowledged checkpoints. Mixed-author evidence is archived without
automatic extraction. Reset clears the target session's author history and turn
counter; rewind preserves conservative attribution history and long-term memory.
An explicit memory delete keeps a scope-bound content-hash tombstone in the ledger
so older admitted events cannot recreate the same content under a different point
ID; a later newly admitted event may intentionally add it again. The tombstone
contains a hash, not the deleted text, and remains until destination reset.
A scoped `delete-all` reuses that fence family: it records a durable deletion
intent, commits a scope-bound tombstone for every enumerated point and for every
prepared write in the scope (including writes whose points were never stored),
invalidates admitted-but-unprocessed turn events for the scope, deletes the
scope in one filtered operation and clears the intent only after the scope is
empty. A scope is one (user, agent) pair; `--all-agents` covers every agent
scope of the user, and a strict run reports `other_agent_scopes` when other
agent scopes still hold memories. A literal `*` is an ordinary agent id and
never selects all agents. A pre-versioned intent cannot show whether `*` is a
single agent or all agents. The plugin refuses it
(`scope_delete_legacy_intent`). Resolve it with
`--resolve-legacy single_agent`, or with `--resolve-legacy all_agents` when the
recorded agent is `*` or absent. Fence rows are durable tombstones — one per
fenced identity, retained until destination reset — and repeated resumes of the
same interrupted deletion reuse identical rows instead of accumulating
duplicates. An interrupted scoped deletion fails closed: other destination
commands and provider startup report `scope_delete_recovery_required` until
`delete-all --confirm` resumes it. Turns admitted after the deletion can still
add new memories. `delete-all` removes or invalidates everything admitted
before it. `list` and `export` read the collection without an embedding service.
An export is a portable JSON document that the Mem0 importer accepts for restore
into a fresh destination (imports re-embed). An export covers stored memories
only. It does not include the SQLite ledger (pending events, retry history) or
crash-recovery state. Back up the profile home for a full recovery point.
Publication refuses to clobber atomically (hard-link). A filesystem without
hard links refuses a non-force export, and `--force` writes without the
guarantee.
System prompt text and tool schemas remain
static through the conversation.

## Backup and restore

### Capacity and retained history

Replay paging bounds transient backlog reads, not the total database or an individual
event/manifest size. Pending/FAILED bodies are recovery inputs. Completed manifests
retain source-plan hashes, operation keys and payload hashes for audit and resume.
Do not delete terminal identity rows, tombstones or manifests as routine compaction.
They protect replay idempotency and later deletion intent. Version 0.1.0 does not
implement automatic retention compaction. It requires a separate design.

Stop the agent for maintenance before you monitor `state.db`, `state.db-wal`,
free disk and destination pending/failed counts. Treat 100,000 pending operations
as the tested synthetic backlog envelope, not a production capacity guarantee.
Benchmark larger workloads before deployment. Set an operational disk alert at
70% usage and pause new writes before exhausting storage. Reserve space for both
a stopped backup and the active database/WAL. Resolve service failures, then
retry explicitly. SQLite may reuse freed pages without reducing file size. A
stopped, consistent backup and SQLite VACUUM can reclaim free pages, but neither
securely erases old backups nor safely authorizes deletion of retained rows.
Never remove WAL/SHM files from a live database. See the acceptance report for
measured sizes and memory.

Stop the agent and every embedded maintenance client before you copy state. Back up
`qdrant-memory.json`, the `qdrant-memory` state directory (including SQLite sidecar
files if present), and the embedded Qdrant path if you configure it elsewhere.
The ledger logically scrubs committed event bodies and committed/superseded operation
bodies, except operations still needed by an incomplete migration manifest. Identity
and status rows, manifests, and pending/failed payloads remain. Recovery needs
pending/failed bodies. Rows and manifests have no time-based expiry, so treat
database files, snapshots, and backups as sensitive conversation data. Keep
credentials in a secret manager rather than a portable plaintext backup.

Restore the store and ledger from the same stopped snapshot into the intended
profile. Restore the matching embedding configuration. Confirm directory and file
permissions. Then run doctor, stats and verify before you enable the provider. A
ledger restored against unrelated target state may replay prepared changes. Do
not mix arbitrary snapshots. Back up remote Qdrant through its deployment's own
snapshot procedure, coordinated with the stopped plugin ledger. Deleting a Qdrant
point does not clear its ledger identity or pending/failed work. `init --existing
clear` removes the chosen destination's ledger rows as part of collection reset.
It does not guarantee secure erasure from SQLite pages/WAL storage or copied
backups.

## Retry and maintenance ownership

If a collection clear does not finish, preserve the ledger. Rerun
`hermes qdrant-memory init` with the same destination configuration (and the same
`--collection` override, if used). The durable reset intent resumes the previously
authorized destructive operation. It is not a request to reuse the remaining data.
Other destination commands report `reset_recovery_required` until recovery completes.
Keep every writer stopped through recovery. Then run `doctor`, `stats` and `verify`.

```bash
hermes qdrant-memory stats
hermes qdrant-memory retry
hermes qdrant-memory verify
```

Failed raw events need the provider's trusted LLM context. Retry only requeues
them for the next provider startup. The CLI can commit prepared operations.
The plugin recovers pending operations automatically on startup. FAILED items
require operator correction and explicit retry. A missing migration operation
invalidates its manifest rather than counting it as completed. A fresh migration
plan can repair an unchanged target with stale open work using a new operation
generation. `SUPERSEDED` migration operations are terminal but do not certify a
successful import. Verification reports `migration_superseded`. Inspect the plan
and later deletion intent before you deliberately start a fresh import without
`--resume`. When failed operations remain, verification raises
`migration_incomplete` before any conflict, and the final progress line reports
the incomplete plan first. Fix failed work. Use `--resume --retry-failed`. This
does not authorize restoring superseded records. See
[migration](migration-from-mem0.md).

## Metrics

Stats reports destination-scoped ledger states and retry totals. It also reports
bounded samples of embedding/query/search/extraction latency, upsert throughput
and cache hit rate when recorded, and persistent ADD/SKIP/UPDATE decision
counters. These count decisions, including replay attempts, rather than unique
logical memories. Each metric retains at most 512 samples. Reported p50/p95
values are empirical sample percentiles, not a service-level guarantee. The
plugin records cache-hit rate during active prefetch calls, and it remains
visible before shutdown.

See [troubleshooting](troubleshooting.md) for mismatch, lock, missing-row and
credential failures, and [architecture](architecture.md) for persistence boundaries.
