# ADR: replay one ambiguous remote write instead of dropping it

Status: accepted for Runtime 0.5.2 and later. Scope: the HTTPS MCP client
adapter and the `/events` client.

## Context

The Owner keeps one active writer and one durable ledger. A supervised restart
stops the HTTP listener and starts it again. A client request can cross that
boundary in three ways:

1. The Owner did not admit the request. No event row and no operation exists.
2. The Owner admitted and committed the request. The response did not arrive.
3. The Owner admitted the request. The commit did not finish before the stop.

The 24-hour cloud soak measured 330 controlled restarts. 23 direct writes never
reached the ledger. The client reported `remote_error` with `retryable: false`
for those rows. A caller that obeys that contract therefore drops the write.
The other restart failures kept the record, and only the response was lost.

The adapter classified only a timeout and a connect failure as retryable. A
protocol reset, an end of stream, a broken MCP stream and a 5xx answer fell into
the generic class. That class says the request is final. This ADR replaces that
class set.

## Decision

The adapter classifies one failure into one reason. Only the reason selects the
retry behavior.

| Reason | Cause | Retryable | Replay key |
| --- | --- | --- | --- |
| `unreachable` | connect error or connect timeout | yes | request or event ID |
| `timeout` | read or write timeout | yes | request or event ID |
| `outcome_unknown` | reset, end of stream, broken stream, lost response | yes | request or event ID |
| `temporarily_unavailable` | 408, 429, 502, 503, 504, or an Owner retry verdict | yes | request or event ID |
| `recovery_required` | the Owner admitted the request and needs operator recovery | no | not applicable |
| `authentication_rejected` | 401 or 403 | no | not applicable |
| `tls_verification_failed`, `tls_failed` | TLS trust or handshake failure | no | not applicable |
| `business_error` | other 4xx or 5xx answer | no | not applicable |
| `protocol_error` | redirect, refused MCP method, invalid MCP envelope | no | not applicable |
| `remote_error` | no other class matches | no | not applicable |

A retryable failure carries a `replay` field. The field names the identifier
that the caller must send again. The value is `request_id` for a tool call and
`event_id` for a lifecycle event. A new identifier can create a second record.
The envelope also carries `retry_after` when the Owner sends that header.

A tool result that the Owner marks `retryable: true` is transient. The adapter
replays it with the same request ID. A tool result that the Owner marks
`retryable: false` is final. The adapter returns that object unchanged.

A lifecycle rejection uses the full envelope, with the Owner type in `type`.
A tool rejection keeps the Owner object, so `recovery_required` is the
lifecycle-path reason for the same state.

An HTTP 4xx answer that is not 401, 403, 408 or 429 is `business_error`.
`protocol_error` covers a redirect and an MCP-level protocol failure. It does
not cover an HTTP answer from the Owner.

The adapter replays a retryable request two times at most. Each replay uses the
same body and the same identifier. A full-jitter wait separates the attempts.
The first wait is 0 to 0.2 s and the second wait is 0 to 0.4 s. The two waits
add up to 0.6 s at most.

The adapter does not replay a failure that needed more than 5 s to return. It
also does not replay inside a `Retry-After` window that is longer than 1 s. A
slow failure and a long rate-limit window belong to the caller's durable
outbox, because the adapter must stay inside the caller's request deadline.

The Owner provides the convergence point. A shared mutation is keyed by
`(owner, space, principal, device, request_id)` in `shared_requests_v1`. A
lifecycle event is keyed by the authority and the `event_id` in
`owner_events_v1`. A replay with the same key returns the stored receipt. It
adds no second point, no second operation and no second event.

The Owner serializes the whole shared call under its Runtime lock. A second
request with the same key waits for the first request and then reads the stored
receipt. The replay therefore cannot observe a half-applied plan.

## Durability stages

A write passes four stages. This ADR does not claim that all four stages are
atomic.

| Stage | Owner of the state | Loss behavior |
| --- | --- | --- |
| Host outbox | the client host | the turn is unsent, and the host must keep it |
| Owner admission | the Owner ledger | no receipt exists, and the client replays the same ID |
| Owner confirmation | the Qdrant destination and the operation row | the operation row keeps the replay contract |
| Client receipt | the client host | a replay reads the receipt again |

A request that did not reach Owner admission is the client outbox's
responsibility. The Runtime cannot store a request that it never received. The
Pi adapter keeps such an event in its session outbox and retries it on the next
turn.

## Quarantine

A deterministic extraction failure still marks only its own event
`QUARANTINED`. Quarantine applies only at the extraction stage, when no
candidate and no operation key exists. A failure after `prepare_event` keeps the
`FAILED` recovery gate. A quarantined event is retained raw input, not a
memory. See `event-quarantine-adr.md`.

## Compatibility and rollback

The change adds reasons and one optional `replay` field to the CLI error
envelope. It does not change a persistent format, a tool schema, a receipt or a
status value. An older client ignores the new fields. A newer client against an
older Runtime keeps the same replay keys, because the request and event tables
are unchanged. Roll back by restoring the previous Runtime. No data migration
is necessary.

## Verification

Unit tests cover each reason, a nested exception group, a lost tool response, a
lost event response, a rejected credential and the retry budget. The lost
response tests use the real Gateway and assert one record, one operation and one
request row after the replay. A separate test kills a real listener during a
request and asserts that the adapter reports a replayable reason.
