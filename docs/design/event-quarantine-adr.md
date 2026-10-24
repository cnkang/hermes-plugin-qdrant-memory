# ADR: quarantine one extraction failure without stopping the Owner

Status: accepted for Runtime 0.5.2 and later. Scope: lifecycle events only.

## Context

The Owner keeps every accepted lifecycle event in the durable ledger before it
calls the extraction model. When extraction fails, the event is marked `FAILED`
and the Owner refuses further shared writes until an operator reconciles it.

That behavior protects durability. It also lets one deterministic model failure
remove memory service from every device. The two-device acceptance run recorded
this outcome: a truncated extraction on the Pi device stopped writes from both
OpenCode devices until the operator raised the token budget, ran `retry` and
restarted the Owner.

## Decision

A deterministic extraction failure marks only its own event `QUARANTINED`. The
Owner keeps accepting unrelated writes. Transient failures keep the existing
`FAILED` recovery gate.

Deterministic reasons are `output_truncated`, `output_invalid` and
`authentication_rejected`. Transient reasons are `timeout`, `provider_unreachable`,
`rate_limited` and `provider_error`.

## Safety argument

Quarantine is allowed only when all of these hold:

1. The failure happened inside the extraction call.
2. No candidate and no operation key was written to the event payload.
3. The event source is an admitted lifecycle, native or checkpoint event.
4. The reason is deterministic, so the same configuration cannot succeed.

The event row keeps its raw turn, its reason code and its attempt count. No
Qdrant point and no ledger operation exists for it, so quarantine cannot leave a
partial write. A retry therefore re-runs extraction from the same durable input.

The Owner still fails closed for every other failure class:

- A failure after `prepare_event` persisted candidates or operation keys marks
  the event `FAILED` and raises the recovery gate. A prepared plan may already
  name a Qdrant write, so the operator must reconcile it.
- A failure in `commit` or in the acknowledgement path marks the event `FAILED`.
  The operation rows carry the replay contract.
- A transient extraction failure keeps the `FAILED` gate, because the provider
  may recover and the operator expects a bounded retry budget.
- An unknown or corrupt event state is never quarantined.

## State transitions

| From | Condition | To |
| --- | --- | --- |
| `PENDING` | extraction succeeds and commit succeeds | `COMMITTED` |
| `PENDING` | deterministic extraction failure, no plan | `QUARANTINED` |
| `PENDING` | transient failure or failure after a plan exists | `FAILED` |
| `QUARANTINED` | operator runs `retry` | `PENDING` |
| `QUARANTINED` | its device cancels the raw input | `CANCELLED` |
| `QUARANTINED` | Owner restart | `QUARANTINED` |

`QUARANTINED` is terminal for service availability and open for operator
recovery. It is not terminal for the durability of the user's text.

## Crash windows

- Crash before the event row exists: the adapter resubmits the same event ID.
- Crash between the event row and the extraction call: the row stays `PENDING`
  and recovery retries it.
- Crash after quarantine: the row stays `QUARANTINED` and recovery skips it.
  Quarantine writes one row update inside one transaction, so it is atomic.
- Crash during a retry: the row is `PENDING` again, and the retry is idempotent
  because the event key and the canonical-text deduplication are unchanged.

## Authorization

Only the owning device may inspect or cancel its event. Cancellation requires a
`QUARANTINED`, `FAILED` or `PENDING` row with no candidates and no operation
keys. `retry` needs the operator policy that the maintenance command already
requires. A client credential cannot clear a quarantine.

## Compatibility and rollback

The change adds one status value and one counter. Existing rows, collections and
receipts keep their meaning. An older Runtime reads `QUARANTINED` as an unknown
status: it skips the row in replay and reports it in `stats`, so a rollback
retains the data but stops automatic retry until the newer build returns.

## Observability

`stats` groups the new status. `shared-doctor` reports a `quarantine` check and
keeps the `recovery` verdict for real pending work. The event status endpoint
returns `QUARANTINED` without content or provider text.
