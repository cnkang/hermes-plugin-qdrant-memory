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

## 6. Twenty-four hour soak

The soak completed the full planned window on the merged main branch. It ran
through one host restart, which the journal survived because each iteration is
appended to `evidence/soak.jsonl`.

| Item | Value |
| --- | --- |
| Planned window | 1440 minutes |
| Observed window | 24.01 hours (86,427 seconds) |
| Attempts | 3096 |
| Successful pairs | 3068 |
| Failed pairs | 28 |
| Success rate | 99.10 % |
| Latency p50 | 1260 ms |
| Latency p95 | 2091 ms |
| Owner restarts | 774 |
| Restart downtime, median | 0.6 s |
| Restart readiness, median | 2.1 s |
| Restart readiness, maximum | 181 s (one cold start) |
| First readiness probe | 0.5 s |
| Ledger size at the end | 6.4 MB |

The harness waited for a successful read before it counted the run as ready. A
request that fails during startup is therefore outside the success rate and is
reported on its own.

The 28 failures need a careful reading:

1. All 28 are the `missing` class. The write call returned, and the immediate
   read did not find the record.
2. All 28 fall within two iterations of a planned Owner restart.
3. No failure produced a permanent loss. The records appear once the Owner is
   ready again.

The distribution by hour is 4 failures in each of hours 1, 7, 11, 14 and 16, and
8 in hour 22. The rate does not grow with uptime. The test keeps every sample and
does not remove a failure from the count.

The 24-hour window therefore meets the completion rule: the full duration ran and
the journal is complete. The result is **24H SOAK PASS** for this synthetic load
on two local cores. It does not by itself prove production reliability.

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

1. Repeat the identical-text case through two agent hosts with a prompt that
   reaches the tool, and keep both tool receipts.
2. Decide whether rotating credentials should treat a 401 as transient.
3. Explain the one-to-two-iteration read miss after a restart, and decide whether
   the client should retry a read inside that window.
