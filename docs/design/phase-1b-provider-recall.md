# Phase 1b — Shared recall in the native provider

Status: design for review (revision 2). Date: 2026-10-19. This document adds to
[universal-cross-agent-memory.md](universal-cross-agent-memory.md).

## 0. Dependency

This phase uses the reader that Phase 1a adds (`qdrant_memory/shared_reader.py` and
`qdrant_memory/shared_protocol.py`). The branch for this phase is stacked on the Phase 1a
branch. Do not merge this phase before Phase 1a.

## 1. Goal

Connect the shared reader to the recall path of the Hermes native provider. The feature
is off by default. When it is off, the provider output and the cache shape do not change.

## 2. Configuration

The provider reads a new `shared_read` block:

| Field | Default | Maximum | Meaning |
|---|---|---|---|
| `enabled` | false | — | Turn shared recall on. |
| `spaces` | [] | 8 items | The granted spaces. |
| `limit` | 4 | 20 | The maximum number of shared items in one recall. |
| `timeout_seconds` | 2 | 10 | The complete shared read must finish in this time. |
| `cache_ttl_seconds` | 300 | 3600 | The maximum age of a cached recall. |
| `include_tool_search` | false | — | Also add shared items to the `qdrant_memory_search` tool result. |

Each item in `spaces` has these fields:

| Field | Meaning |
|---|---|
| `space_id` | The space name. It must be unique in the list. |
| `visibility` | `shared` or `project`. This phase rejects `private`. |
| `url` | A literal loopback URL, for example `http://127.0.0.1:8765`. |
| `token_env` | The name of the environment variable that holds the token. The name must match `^[A-Z][A-Z0-9_]{2,63}$`. |

The configuration load rejects these cases:

- `enabled` is true and `spaces` is empty.
- An item has a token value instead of the name of an environment variable.
- A visibility is `private` or not in the list above.
- Two items have the same space.
- A URL is not a literal loopback URL.
- `limit`, `timeout_seconds` or `cache_ttl_seconds` is out of range.
- The private `scope.agent_id` starts with `shared:v1:`.

The token stays in the environment. The provider never writes the token to a file, a log
or an error message.

## 3. Read path

The background recall job does these steps:

1. Run the private search. Do not change this step.
2. If `shared_read.enabled` is false, store the private text as it does today and stop.
3. Read the shared spaces with the configured limit and timeout. The read runs in a
   separate single-thread executor. The job waits at most `timeout_seconds` for the
   complete read.
4. Merge the results. Private items come first. Shared items follow, and each one has the
   label `[shared:<space_id>]`.
5. Limit the merged text to 4000 characters.
6. Store the merged text in the recall cache.

The reader revalidates each item against the configured space, the visibility and the
`shared_protocol` marker. An item that fails the check is not used.

A shared read does not run during shutdown. The job checks the stop flag before it starts
the read.

## 4. Tool search

`handle_tool_call` for `qdrant_memory_search` keeps its current result when
`include_tool_search` is false.

When the flag is true, the result gets one more key, `shared`, with the same item shape
and the space label. The private part of the result does not change. A shared failure
does not change the private part and does not fail the call.

## 5. Failure behavior

| Condition | Result |
|---|---|
| The owner is not available | The private text stays. The failure counter increases. |
| The timeout expires | The private text stays. The failure counter increases. |
| A credential is expired or revoked | The private text stays. The failure counter increases. |
| A response is malformed or too large | The private text stays. The failure counter increases. |
| One space fails and another succeeds | The successful items are used. The failure counter increases. |
| All spaces fail | The private text stays. |

A failure does not cause a retry in the same turn. The next recall tries again. A failure
does not remove private recall.

## 6. Cache

When the feature is off, the cache entry is `(scope, text)` and `prefetch` compares the
scope only, as it does today.

When the feature is on, the entry is `(scope, digest, expires_at, text)`. `prefetch`
returns the text only when the scope matches, the digest matches and the entry has not
expired. A mismatch gives an empty result, and the normal prefetch flow fills the cache
again.

The digest is a SHA-256 hash of the `shared_read` block with the token names, the URLs,
the spaces, the visibility and the limits. It does not contain a token value.

A local cache cannot see a revocation on the owner. The cache lifetime is therefore the
maximum staleness: `cache_ttl_seconds`. A revocation takes effect on the next recall that
reaches the owner, and no later than the end of the cache lifetime. This phase does not
claim that a revocation removes text that a host already put into the model context. The
host must clear or rebuild the affected context, as the Phase 0 notes state.

## 7. Write path

Shared recall is read-only. The provider does not call add, update or delete on a shared
space in this phase. It does not open the owner store and does not hold a write lease for
shared data.

## 8. Tests

1. With the feature off, the recall text and the cache shape are identical to the current
   behavior.
2. With the feature on, the text contains the private items first and the labeled shared
   items after them.
3. A timeout, an error, a malformed response and a partial failure each give the expected
   result and increase the counter.
4. The configuration load rejects each case in Section 2.
5. The private `scope.agent_id` `shared:v1:project` is rejected.
6. A digest change and an expired entry each cause a cache miss.
7. The provider makes no write call during shared recall.
8. Shared text is absent from the private store, the ledger and the events.
9. Shutdown does not start a shared read.
10. With `include_tool_search` off, the tool result is identical to the current result.

## 9. Non-goals

This phase does not include shared writes, automatic extraction into shared spaces, the
remote transport, the identity model or a change to the private memory pipeline.
