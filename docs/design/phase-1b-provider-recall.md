# Phase 1b — Shared recall in the native provider

Status: implemented source preview (revision 3). Date: 2026-10-20. This document adds to
[universal-cross-agent-memory.md](universal-cross-agent-memory.md).

## 0. Dependency

This phase uses the reader in main (`qdrant_memory/shared_reader.py` and
`qdrant_memory/shared_protocol.py`), including the parser-redaction fixes from PR 63.
It is the first review layer of the Universal Owner PR stack.

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
| `timeout_seconds` | 2 | 10 | Upper bound for a shared read; reduced by a caller delivery budget. |
| `delivery_timeout_seconds` | 0.05 | 0.25 | Maximum synchronous recall wait; network deadlines use the smaller budget. |
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
- `limit`, `timeout_seconds` or `delivery_timeout_seconds` is out of range.
- An unsupported setting such as `cache_ttl_seconds` is present.
- The private `scope.agent_id` starts with `shared:v1:` or `shared:v2:`.

The token stays in the environment. The provider never writes the token to a file, a log
or an error message.

## 3. Read path

The existing background job still fills only the private cache. When the feature is
enabled, synchronous `prefetch` does these steps:

1. Snapshot the private cached text, trusted scope and session generation.
2. Start one shared read in a separate single-thread executor, using the smaller of
   `timeout_seconds` and `delivery_timeout_seconds` as its wait/network deadline.
3. If another shared read is still outstanding, return without queueing stale work.
4. After the wait, check the trusted scope and generation again. An author/session
   change discards the entire earlier result, including its private text.
5. Put private text first (at most 2000 characters), then a framed JSON array of shared
   items, keeping the complete output within 4000 characters. Each item retains its
   space, visibility, version and provenance; long text is truncated inside the JSON.
6. Discard late shared results. Do not store shared text in the private cache or store.

The reader revalidates each item against the configured space, the visibility and the
`shared_protocol` marker. An item that fails the check is not used.

A shared read does not start after shutdown. Closing the reader stops admission;
outstanding read-only I/O cannot access the private Runtime resources.

## 4. Tool search

`handle_tool_call` for `qdrant_memory_search` keeps its current result when
`include_tool_search` is false.

When the flag is true, the result gets one more key, `shared`, with the same item shape
and space/provenance fields. The private part of the result does not change. A shared failure
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

When the feature is on, the private entry still contains only private text. Shared
results are never cached. Each delivered shared recall requires a fresh Owner request
and matching trusted session identity before and after I/O. Revocation/deletion therefore
takes effect on the next request reaching the Owner. This does not remove text that a
host already put into model context; the host must clear or rebuild that context.

## 7. Write path

Shared recall is read-only. The provider does not call add, update or delete on a shared
space in this phase. It does not open the owner store and does not hold a write lease for
shared data.

## 8. Tests

1. With the feature off, the recall text and the cache shape are identical to the current
   behavior.
2. With the feature on, private items precede a complete shared JSON array with provenance.
3. A timeout, an error, a malformed response and a partial failure each give the expected
   result and increase the counter.
4. The configuration load rejects each case in Section 2.
5. Private configuration rejects both reserved Owner namespace prefixes.
6. Revoked/deleted shared results are absent from the next freshly authorized recall;
   author changes during I/O cannot deliver earlier private text.
7. The provider makes no write call during shared recall.
8. Shared text is absent from the private store, the ledger and the events.
9. Shutdown does not start a shared read.
10. With `include_tool_search` off, the tool result is identical to the current result.
11. The configured delivery budget reaches the bounded wait without relying on thread
    scheduling; late results are discarded and do not accumulate in a queue.
12. Long shared text keeps complete JSON framing and provenance under the context limit.

## 9. Non-goals

This phase does not include shared writes, automatic extraction into shared spaces, the
remote transport, the identity model or a change to the private memory pipeline.
