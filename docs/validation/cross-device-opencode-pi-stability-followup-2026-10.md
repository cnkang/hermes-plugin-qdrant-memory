# Cross-device stability follow-up (2026-10-25)

This note adds the evidence that the first stability report left open. It keeps
the earlier results and records new runs on the merged main branch.

## 1. Date and baseline

| Item | Value |
| --- | --- |
| Run date | 2026-10-25 |
| Runtime main | `1acfeb8` (event quarantine merged) |
| MCP main | `d4a0218` (Pi quarantine acknowledgement merged) |
| Skill | `da19b4b`, version 0.3.4 |
| Released Runtime | 0.5.1 from PyPI |
| Released MCP | 0.2.5 |

The acceptance Owner ran the main source through `PYTHONPATH`. The released
comparison installed the 0.5.1 wheel into a separate environment.

## 2. Devices

| Item | Device A | Device B |
| --- | --- | --- |
| System | macOS 27.0.1, arm64 | Omarchy Linux, x86_64 |
| OpenCode | 2.0.24 | 2.1.3 |
| Pi | not used | 1.4.0 |
| Role | Owner, OpenCode A, TLS proxy | OpenCode B, Pi, outbox |

## 3. Concurrency (D2)

| Case | Result | Evidence |
| --- | --- | --- |
| Identical text, two credentials, one barrier | PASS | First call `ADD`, second `SKIP`, one record, same record id |
| Identical text through two agent hosts | NOT VERIFIED | The agent read the Skill and answered without calling the add tool |
| Similar but distinct facts | PASS | Three distinct records stored |
| Competing updates on one version | PASS | One update, one conflict, winner text stored |
| Independent parallel updates | PASS | Two updates, both committed |
| Ledger after the group | PASS | Operations all `COMMITTED`, events all `COMMITTED` |

The competing-update and independent-update cases ran through the two OpenCode
hosts at the same time. The barrier case used the service interface because the
agent prompt did not reach the tool.

## 4. Released baseline against main (D6)

| Behaviour | Released 0.5.1 | Main `1acfeb8` |
| --- | --- | --- |
| Explicit add and search | PASS | PASS |
| Deterministic extraction failure | Event `FAILED`, owner blocked | Event `QUARANTINED`, writes continue |
| Write after the failure | `SharedRecoveryRequiredError` | Accepted |
| Operator retry | Commits after restart | Commits once after restart |

The released run installed `hermes-plugin-qdrant-memory==0.5.1` from PyPI into a
new environment and reported the event status `FAILED` with `blocked=true`. The
main run is recorded in the first stability report and in the quarantine drill.

The released distribution pins Runtime 0.5.1. A user who installs 0.2.5 gets the
`FAILED` gate until a later Runtime release carries quarantine.

## 5. Failure and recovery

The crash, restart, retry, replay and cancellation cases are covered by the
Runtime suite. The cross-device drill added the same-event retry, a second retry
with no new record, and the continue-write case during quarantine.

| Case | Result |
| --- | --- |
| Quarantine survives restart | PASS |
| Retry commits exactly one record | PASS |
| Second retry adds no record | PASS |
| Device cannot cancel another device's event | PASS |
| Prepared plan keeps the fail-closed gate | PASS |
| Transient provider failure keeps the gate | PASS |

## 6. Long soak

The 24-hour run did not complete in this round. The earlier 20-minute run stands:
42 write and read pairs, 10 restarts, read latency p50 1093 ms and p95 1162 ms.
Four attempts fell inside the first cold start and produced no record.

The harness supports a fixed duration, a fixed seed, bounded request rates, a
readiness probe, periodic restarts and a final summary. A 24-hour run needs a host
that stays up for the full window. This round did not have that window, so the
result is **PARTIAL**, not PASS.

## 7. Review state after the merge

| Source | State |
| --- | --- |
| CodeRabbit on Runtime #95 | Approved at 2026-10-25T12:04:06Z |
| Kiro CLI | Second round: no P0, two P2 |
| OpenCode | Second round: no P0, one P1 and several P2 |
| Sourcery on MCP #45 | One release-order note, answered and resolved |

## 8. Documents corrected

1. The first stability report listed both pull requests as open. They are merged.
2. The same report listed CodeRabbit as not verified. It approved the Runtime
   change before the merge.
3. The report described the release pin as a defect in the adapter change. It is
   a release-order dependency.

## 9. Remaining work

1. Run the 24-hour soak on a host that stays up.
2. Repeat the identical-text case through two agent hosts with a prompt that
   reaches the tool.
3. Decide whether rotating credentials should treat a 401 as transient.
