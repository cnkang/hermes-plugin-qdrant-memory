# Architecture and contracts

The plugin stays outside Hermes core. Directory discovery calls the root
`register(ctx)`; packaged discovery calls `qdrant_memory.register(ctx)`. Both use
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
| cli / config_schema | Maintenance commands and non-secret setup persistence |
| models / config / retry | Payload identity, validated settings and sanitized transport retries |

## Turn persistence and ownership

1. A completed primary-agent turn enters a small synchronous SQLite transaction.
2. A serial worker receives the event key together with the caller's copied context.
3. Extracted candidates are persisted before relation checks and vector writes.
4. Prepared operation keys are persisted before committing to Qdrant.
5. The ledger acknowledges the mutation only after a `wait=True` write succeeds.

An exit between steps 4 and 5 leaves replayable work. An exit after Qdrant upsert
but before acknowledgment repeats the same prepared point ID instead of creating
another logical record. Events can remain FAILED after service or schema errors;
an explicit retry requeues them. Replay of other pending items continues when one
item fails. The ledger is not an unlimited guarantee against disk failure or loss
of the ledger itself.

Provider and CLI mutations share `Runtime.lock`. The worker owns connections after
successful initialization; shutdown stops new admission and briefly waits for the
drain. If it exceeds the timeout, shutdown raises and pending durable work remains
replayable. A local OS writer lease prevents another provider/CLI from overlapping
the retiring worker on the same profile destination. Cleanup holds the runtime lock
so foreground tool calls cannot race connection closure.
During failed initialization, created resources are closed immediately.

## Scope, cache and prompt invariants

Every retrieval filters `user_id` and `agent_id`; a null agent has an explicit null
condition. Exact-ID reads check payload scope before update/delete. A tool cannot
submit a scope override. Non-bot gateway author identity takes precedence for that
turn. Mixed-author session transcripts are not attributed to the latest author.

Prefetch runs in the worker. `prefetch()` itself reads only a matching cached value
or returns an empty string. Each queued search has a generation; session/author
changes invalidate the generation so late results cannot repopulate stale recall.
System prompt text and tool schemas remain static throughout the conversation.

## Pipeline and mutation identity

The collection's reserved point records the embedding fingerprint. Startup probes
the configured dimension and validates the named `dense` vector and distance.
Unknown nonempty collections and fingerprint mismatches are refused. Fingerprints
include model/provider, endpoint, dimensions, metric, instruction prefixes and
dimension-sending policy; credentials are excluded.

Point UUIDs include user/agent scope and source identity. Operation keys additionally
include destination namespace and prepared payload. The destination namespace binds
the collection to its backend/path, preventing replay into another endpoint with
the same collection name. New committed work supersedes earlier open writes to the
same point. Migration repair uses a new generation when stale open work exists,
even if the target already contains the incoming payload.

## Migration and compatibility boundaries

Migration never semantically merges distinct source IDs. It preserves source metadata,
re-embeds texts and retains a durable plan with expected IDs and payload hashes.
Changed source snapshots create new plans; a missing ledger operation is an explicit
failure, not a completed migration. Collection counts supplement exact verification.

v0.1 supports dense retrieval only. Sparse/hybrid retrieval, reranking and recency
weighting are future work. Host embedding inheritance is feature-detected and requires
an explicit fallback. The minimum complete Hermes contract is v2026.9.24; earlier
tags lack required authoritative builtin replacement metadata. Live Server REST/gRPC
tests and optional authenticated Cloud tests remain separate validation lanes.
