# Architecture and contracts

The plugin stays outside Hermes core. Directory discovery calls the root
`register(ctx)`. Packaged discovery calls `qdrant_memory.register(ctx)`. Both use
the same provider factory. When the discovery collector lacks `llm`, the factory
creates a named Hermes `PluginContext` and registers extraction's auxiliary task
there. Model selection, credentials and operator trust remain host-owned.

## Components

| Module | Responsibility |
| --- | --- |
| provider | Lifecycle hooks, profile/session/author scope, serial worker and recall cache |
| runtime | Durable preparation, ordered mutations, replay and relation decisions |
| ledger | Private SQLite event/operation state, manifests and bounded metrics |
| qdrant_store | Named dense collection, pipeline identity, scoped query and exact-ID access |
| embedding | HTTP pipeline adapters, response-order and vector validation |
| extraction / dedupe | Host LLM extraction and factual relation adjudication |
| migration | Read-only sources, source-ID mapping, resumable plans and exact verification |
| reset | Durable destination reset intent, fail-closed admission and interrupted reset recovery |
| cli / config_schema | Maintenance commands and non-secret setup persistence |
| models / config / retry | Payload identity, validated settings and sanitized transport retries |

## Turn persistence and ownership

Hermes currently submits `sync_turn` through its in-memory background executor.
This plugin's durability guarantee does not cover the completed turn while it
waits in that host queue. Abrupt process exit or bounded host shutdown can discard
a callback that has not started. The plugin boundary begins when Hermes invokes
`sync_turn` and the provider commits the event to SQLite.

1. The admitted primary-agent turn enters a small synchronous SQLite transaction.
2. A serial plugin worker receives the persisted event key and caller context.
3. The worker persists extracted candidates before relation checks and vector writes.
4. It persists prepared operation keys before committing to Qdrant.
5. The ledger acknowledges the mutation only after a `wait=True` write succeeds.

An exit between steps 4 and 5 leaves replayable work. An exit after Qdrant upsert
but before acknowledgment repeats the same prepared point ID instead of creating
another logical record. Events can remain FAILED after service or schema errors.
An explicit retry requeues them. Replay of other pending items continues when one
item fails. This recovery guarantee starts at plugin ledger admission and does not
cover Hermes's earlier in-memory queue. The ledger is not an unlimited guarantee
against disk failure or loss of the ledger itself.

Provider and CLI mutations share `Runtime.lock`. The worker owns connections after
successful initialization. Shutdown stops new admission and briefly waits for the
drain. If it exceeds the timeout, shutdown raises and pending durable work remains
replayable. A local OS writer lease prevents another provider/CLI from overlapping
the retiring worker on the same profile destination. Cleanup holds the runtime lock
so foreground tool calls cannot race connection closure.
During failed initialization, the plugin closes created resources immediately.

The OS lease is local process coordination, not a distributed lock contract. It
does not establish exclusion across separate profile homes on the same machine or
writers on other machines, or make shared absolute storage paths safe. Run one
writer per destination. Stop writers across all profiles and hosts before mutating
maintenance operations.

## MemoryProvider callbacks and manifest hooks

Both directory and wheel entry points register a `MemoryProvider`. This plugin does
not call `PluginContext.register_hook`. Its v2 manifests therefore declare an empty
generic `provides_hooks` list. The implemented `MemoryProvider` lifecycle methods
are `is_available`, `unavailable_reason`, `initialize`, `system_prompt_block`,
`prefetch`, `queue_prefetch`, `sync_turn`, and `shutdown`. The session hooks are
`on_turn_start`, `on_session_end`, `on_session_switch`, `on_pre_compress`, and
`on_memory_write`. The provider also supplies `get_tool_schemas`,
`handle_tool_call`, `get_config_schema`, and `save_config`. These provider
callbacks are a separate host interface, not
generic plugin event hooks.

The v2 `python_dependencies` metadata repeats the runtime requirements from
`pyproject.toml` for discovery. Hermes surfaces this field but does not install from
it. Hermes PM installation continues to use the project dependency declaration.

## Scope, cache and prompt invariants

Every retrieval filters `user_id` and `agent_id`. A null agent has an explicit null
condition. Exact-ID reads check payload scope before update/delete. A tool cannot
submit a scope override. Non-bot gateway author identity takes precedence for that
turn. Current-turn memory eligibility is separate from that stored human scope:
bot and unidentified shared-session turns cannot use it. Explicit authors on
asynchronously completed human turns retain their own write scope.
The plugin does not attribute mixed-author session transcripts to the latest
author. Bot or unidentified participation also disables supplementary transcript
extraction.
The ledger persists that quarantine across restart until an authorized transcript
reset. Continuation IDs inherit the parent's scope, author history, active denial,
and durable quarantine. Checkpoint archival continues with neutral attribution
and no extraction.

Prefetch runs in the worker. `prefetch()` itself reads only a matching cached value
or returns an empty string. Each queued search has a generation. Session/author
changes invalidate the generation so late results cannot repopulate stale recall.
System prompt text and tool schemas remain static throughout the conversation.

## Pipeline and mutation identity

The collection's reserved point records the embedding fingerprint. Startup probes
the configured dimension and validates the named `dense` vector and distance.
The plugin refuses unknown nonempty collections and fingerprint mismatches.
Fingerprints include model/provider, endpoint, dimensions, metric, instruction
prefixes and dimension-sending policy. The fingerprint excludes credentials.

Point UUIDs include user/agent scope and source identity. Operation keys additionally
include destination namespace and prepared payload. The destination namespace binds
the collection to the canonical `backend_destination` identity, a hash of the
normalized endpoint address (or embedded path). Mode labels and URL aliases
(for example, server `https://x` against cloud `https://x:443/`) resolve to the
same namespace.
This prevents replay into another endpoint with the same collection name. New
committed work supersedes earlier open writes to the same point. Migration repair
uses a new generation when stale open work exists,
even if the target already contains the incoming payload.

## Migration and compatibility boundaries

Migration never semantically merges distinct source IDs. It preserves source metadata,
re-embeds texts and retains a durable plan with expected IDs and payload hashes.
Changed source snapshots create new plans. A missing ledger operation is an explicit
failure, not a completed migration. Collection counts supplement exact verification.
`SUPERSEDED` operations are terminal conflicts, not successful writes. Migration
verification raises a sanitized conflict. Resuming an old plan does not authorize
restoring memories deleted later. Recovery scans use bounded rowid pages and a finite
watermark. This limits transient reads, not retained database or manifest size.

Collection clearing commits a destination reset intent before deleting Qdrant data.
The intent blocks ordinary destination use until `init` completes collection rebuild,
identity validation and destination ledger cleanup. An operator can resume
interrupted recovery itself. Other destinations and transcript quarantine remain
intact. Collection
deletion/recreation is recoverable rather than atomic.

v0.1 supports dense retrieval only. Sparse/hybrid retrieval, reranking and recency
weighting are future work. Host embedding inheritance is feature-detected and requires
an explicit fallback. The minimum supported Hermes release is v0.21.5 (v2026.9.24);
earlier tags lack required authoritative builtin replacement metadata. Live Server REST/gRPC
tests and optional authenticated Cloud tests remain separate validation lanes.
Required compatibility CI validates the minimum supported release and the latest
Hermes `main`, recording the checked-out `main` SHA in each run summary. The
`MemoryManager.sync_all()` host-lifecycle check runs as a weekly/manual tracker on
top of the required latest-main lane.
