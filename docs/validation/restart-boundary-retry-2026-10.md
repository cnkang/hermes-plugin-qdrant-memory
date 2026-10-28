# Restart-boundary write recovery (2026-10)

This record covers the fix for the retryable-contract gap in the
[24-hour cloud soak](cross-device-cloud-soak-2026-10.md). That run found 23
restart-boundary writes that never reached the ledger. The client reported
`remote_error` with `retryable: false` for those rows, so a caller that obeys
the contract dropped them.

## Change under test

The remote client now separates an interrupted request from a rejected request.
A retryable class carries the identifier to replay. The adapter replays a fast
failure two times with the same identifier. The Owner answers a replay from its
stored receipt, so the write converges on one record. The decision record is
[remote write retry](../design/remote-write-retry-adr.md).

No persistent format, tool schema or status value changed.

## Local evidence

| Check | Command | Result |
| --- | --- | --- |
| Failure classification matrix, 27 cases | `pytest tests/standalone/test_remote_retry_contract.py` | PASS |
| Lost tool response on a real Gateway | same file | PASS, one record, one operation, one request row |
| Lost event response on a real Gateway | same file | PASS, one event, one plan row, one memory |
| Rejected credential on the real Gateway | same file | PASS, one attempt |
| Retry budget and jitter | same file | PASS, three attempts, two bounded waits |
| Slow failure is not replayed | same file | PASS, one attempt, no wait |
| Killed listener during a request | `pytest tests/test_restart_boundary.py` | PASS, retryable with `replay: event_id` |
| Full local suite with a Qdrant container | canonical runner, `-j 1` | PASS, 1139 passed, 0 failed, 11 gated skips |
| MCP SDK 2.3.0 with httpx2 | five remote test files | PASS, 76 passed |
| Lint | `ruff check .` and `ruff format --check .` | PASS |
| Documentation links | `check_markdown_links.py` | PASS |

The lost-response tests use the real Gateway, the real ledger, a real Qdrant
store and the real remote client. They do not use a stubbed Owner.

## Not verified

These items were not run. They are not PASS results.

- A repeated 24-hour cloud soak. The two cloud hosts were unreachable from the
  test network during this work. Status: NOT VERIFIED.
- A full process restart under TLS with a real Owner profile and 330 controlled
  restarts. Status: NOT VERIFIED.
- The four harness defects from the 24-hour record. The harness is not in this
  repository, and the cloud hosts were unreachable. These items stay open:
  - the executable bit on `report.py`
  - journal archiving on resume instead of deletion
  - `try/except` around the coordinator branches
  - a random marker suffix for retrieval checks

  Status: NOT VERIFIED.
- Host and platform CI for this change. The result arrives after the pull
  request runs. Status: PENDING.

## Residual risk

- A caller that ignores `retryable` still drops an admitted write. The Client
  contract is the only place that can change this behavior.
- A request that the Owner admitted and did not commit needs operator recovery.
  The client reports `recovery_required` and must keep the original identifier.
- The adapter does not replay a slow failure. A caller without a durable outbox
  must implement its own replay for that case.
- A tool-call replay depends on the caller sending the same `request_id`. The
  Owner rejects a changed payload under a used identifier.
