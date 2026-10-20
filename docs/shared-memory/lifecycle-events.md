# Owner lifecycle event controls (source preview)

The opt-in lifecycle extension supports durable `turn_completed` input and
device-bound status/cancellation. The four MCP memory tools are unchanged.
Enable lifecycle explicitly and use the protected HTTPS Gateway. The loopback
JSON memory API is not a public event API.

An adapter submits a stable event ID and permitted user/assistant content to
`POST /events`. The authenticated Owner binds identity, space and capture rule.
`POST /events/manage` accepts only `action` (`status` or `cancel`) and `event_id`.
It reuses authentication, Host/Origin restrictions, rate/concurrency/body limits
and the Runtime lock. No payload can choose an owner, device, agent or scope.

For the CLI, keep credentials in the configured secret environment, never arguments:

```sh
printf '%s' '{"event_id":"host-turn-id"}' | qdrant-memory remote-event \
  --url https://memory.example.com/mcp --action status
printf '%s' '{"event_id":"host-turn-id"}' | qdrant-memory remote-event \
  --url https://memory.example.com/mcp --action cancel
```

The default `remote-event --action submit` remains backward compatible.
The reverse proxy must route `/events/manage` as well as `/mcp` and `/events`.
Use the same private service account, TLS and header policy for all three paths.

Status results contain only `event_id` and a status. They never expose raw memory,
extraction candidates or provider error messages. Another credential identity or
device receives `NOT_FOUND`, including devices that share logical private recall.
A rotated credential may control its previous events only when the operator has
kept the exact trusted device/principal/space/capture-rule mapping.

Cancellation is conservative:

- Only a raw `PENDING` or `FAILED` event with no persisted extraction candidates or
  operation plan can be cancelled. The transaction marks it `CANCELLED` and scrubs
  both the event body and durable admission plan. Its input hash remains for ID reuse checks.
  An atomic status/body comparison rejects a plan changed since inspection.
- A prepared, committed or uncertain write cannot be cancelled. Reconcile through
  existing recovery/CAS semantics. Cancellation never pretends to roll back Qdrant.
- Calls serialize with extraction/writes. An in-flight extraction may finish before
  cancellation obtains the lock, in which case cancellation is rejected.
- A receipt in the crash-before-enqueue gap reports `ACCEPTED`. Recover it before
  cancellation. Cancelling an extraction failure does not automatically clear an
  existing Owner recovery gate. Explicit operator recovery/restart remains required.
- Replay of the same cancelled input returns `CANCELLED`. Changed input with the same
  event ID is rejected. Runtime recovery and `retry_failed` cannot resurrect cancellation.

Pi records `CANCELLED` as a distinct terminal outbox acknowledgement, never as a
committed write. Hosts without this extension remain explicit MCP/assisted clients.
Additional generic task/pre-compact lifecycle semantics remain separate work.

CLI control failures preserve only redacted machine error types and retryability. Remote message/body text is never reflected, including malformed success envelopes.

Standalone and SDK1/SDK2 tests cover authorization, forbidden scope overrides,
prepared-write refusal, raw-body scrubbing, actual SQLite reopen, CLI dispatch,
HTTP request/response handling and Pi outbox replay. These deterministic tests
do not prove model accuracy or public deployment readiness.
