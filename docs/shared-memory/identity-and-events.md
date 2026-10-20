# Device identity and durable event core

This source-preview layer extends the existing single Memory Owner and Runtime.
It does not add a listener or a second memory writer. HTTPS transport and Native
host integration are separate layers of the Universal Owner stack.

## Explicit device grants

Existing six-field grants remain supported. Modern grants additionally pair
`logical_agent_id` with `device_id`. A logical agent can use its authorized private
space from multiple devices, each with an independent credential and principal.
Another logical agent cannot use that private partition. A device or principal
cannot be rebound to a conflicting identity within the policy.

Optional `expires_at`, `revoked`, and `capture_rule_id` fields are validated.
Every request reloads the policy when a loader is configured. Shared automatic
capture requires its own audited capture rule. Permission to read a project does
not authorize capturing private host turns into that project.

Use `shared-device validate --policy PATH`, or explicitly select `--owner-id` and
`--device-id` for `revoke` and `rotate`. Rotation requires a new `--output` token
file. Policy updates use private atomic files and a lock, preserve other devices,
and fail closed on unsupported protected-file behavior. Do not put bearer tokens
in command arguments or commit them to the repository.

## Event admission and recovery

`OwnerLifecycle` is an internal adapter for trusted host hooks. Authorization
fixes the Owner, space, visibility, logical identity, and device. Submitted event
content cannot choose those fields. An immutable admission plan is durable before
enqueueing, so recovery can resume a crash between admission and event creation.
The existing Runtime performs extraction, scoped deduplication, CAS, idempotent
operations, and writes. Modern private dedupe cannot promote unmarked legacy data.

Event inspection and cancellation require the original authorized device. Only
unprepared PENDING/FAILED raw events can be cancelled. Cancellation atomically
checks the ledger status and payload before scrubbing raw content and the receipt. It is terminal across retry and restart. Prepared, committed, or uncertain writes
must recover instead. Cancelling a raw event does not clear a recovery gate.

Explicit Owner LLM settings bound output tokens and reject truncated extraction
responses. External private-content endpoints require TLS. Local loopback model
endpoints may use HTTP. Quoted instructions do not establish a user's preference.
The model evaluation remains a pilot rather than production accuracy acceptance.
