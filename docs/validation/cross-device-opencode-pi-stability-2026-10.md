# Cross-device stability acceptance (2026-10)

This report records the stability work that follows the
[first cross-device run](cross-device-opencode-pi-2026-10.md). The first run
verified Runtime 0.5.0. This run verifies a development branch that adds
event-level fault isolation.

## 1. Baseline

| Component | Released baseline | Reviewed branch |
| --- | --- | --- |
| Runtime | 0.5.1, tag `v0.5.1`, commit `40ac832` | branch from `9c1e0d6` |
| MCP distribution | 0.2.5, commit `fb31ca3` | Pi adapter branch `3f4b904` |
| Skill | 0.3.4, commit `da19b4b` | unchanged |
| Launcher | 0.1.0-rc.6 | unchanged |
| DSH bundle | 0.1.4 | unchanged |

The runtime acceptance used the branch source through `PYTHONPATH`. The clients
on device B used the released 0.5.1 package for transport calls. The Pi adapter
under test ran from the MCP branch.

## 2. Devices and environment

| Item | Device A | Device B |
| --- | --- | --- |
| System | macOS 27.0.1, arm64 | Omarchy Linux, x86_64 |
| Python | 3.13 | 3.14.8 |
| Node.js | 22.23.2 | 26.12.0 |
| OpenCode | 2.0.24 | 2.1.3 |
| Pi | not used | 1.4.0 |
| Role | Owner, OpenCode A, TLS proxy, extraction model | OpenCode B, Pi, outbox |

The Owner used embedded Qdrant storage, `qwen3-embedding:4b` at 2560
dimensions, and `deepseek-v4.1-flash:cloud` with `json_schema` output. The
transport was HTTPS on port 9443 with a private CA and a same-host proxy to the
loopback Owner. Each client used its own device credential.

## 3. Test groups

| Group | Scope | Result |
| --- | --- | --- |
| D1 | Bidirectional create, read and modify across three agents | 9/9 PASS |
| D3 | Credentials, private spaces, foreign IDs, revocation, transport checks | 9/9 PASS |
| D4 | Pi automatic capture, recall, supersession, no re-capture | 6/6 PASS |
| D5 | Event quarantine, continued writes, retry, restart, idempotency | 7/7 PASS |
| D2 | Concurrency and duplicate submissions | NOT VERIFIED in this round |
| D6 | Released 0.5.1 against the branch | NOT VERIFIED in this round |

D2 passed 5/5 in the 0.5.0 run. The code that it covers did not change in this
branch. The group was not repeated because the branch changes the failure path,
not the write path.

## 4. Root cause of the blocking failure

`Runtime.process_event` marked any extraction exception terminal and re-raised it.
`OwnerLifecycle.submit` then set `owner.blocked`, and every shared write returned
`SharedRecoveryRequiredError` until an operator reconciled the event.

The extraction call is the only step before any persisted plan. A deterministic
model failure at that point cannot leave a partial write. The global gate was
therefore wider than the risk required.

## 5. State machine and invariants

| From | Condition | To |
| --- | --- | --- |
| `PENDING` | commit succeeds | `COMMITTED` |
| `PENDING` | deterministic extraction failure, no plan | `QUARANTINED` |
| `PENDING` | transient failure or failure after a plan | `FAILED` |
| `QUARANTINED` | operator `retry` | `PENDING` |
| `QUARANTINED` | device cancels its raw input | `CANCELLED` |
| `QUARANTINED` | restart | `QUARANTINED` |

Invariants:

1. Quarantine requires an empty plan. The ledger refuses a row that holds
   `candidates` or `operation_keys`.
2. Quarantine keeps the raw turn. Only commit and cancellation scrub it.
3. A retained event never blocks unrelated writes. `blocked` stays false.
4. A prepared plan still fail-closes. The generic failure path is unchanged.
5. Retry reuses the event key. Canonical-text deduplication prevents a second
   record.

## 6. Behaviour before and after

| Scenario | Runtime 0.5.1 | Branch |
| --- | --- | --- |
| Pi extraction truncates | Event `FAILED`, all writes blocked | Event `QUARANTINED`, writes continue |
| OpenCode A writes after the failure | Rejected | Accepted |
| Operator fixes the budget and retries | Commit after restart | Commit after restart, one record |
| Provider timeout | `FAILED`, writes blocked | `FAILED`, writes blocked |
| Failure after candidates persist | `FAILED`, writes blocked | `FAILED`, writes blocked |

## 7. Fault drill evidence

The drill used a deterministic injection service on the extraction endpoint. It
returned a truncated structured answer with `finish_reason=length`.

| Step | Observed |
| --- | --- |
| Pi capture with the fault endpoint | Event `QUARANTINED`, `attempts=1`, reason `output_truncated` |
| OpenCode A write during quarantine | Accepted, one record |
| OpenCode B write during quarantine | Accepted, one record |
| Cross-device read of both records | Both found through an independent reader credential |
| Restore the model, stop, `retry --policy`, start | Event `COMMITTED`, `attempts=1` |
| Second retry | No new record, count stays 1 |
| Record provenance | `created_by=pi-b`, source `owner_lifecycle` |

The client outbox treats `QUARANTINED` as a terminal acknowledgement. The adapter
test fails without that change and passes with it.

## 8. Not verified or blocked

- D2 concurrency was not repeated on the branch.
- No two-device run of the released 0.5.1 was executed this round. Its behaviour
  is covered by the 0.5.0 record and by the regression suite.
- The soak ran for a bounded window only. It does not support a claim about
  unattended production use.
- CodeRabbit did not review this change. The repository has no CodeRabbit check
  and the local CLI is not configured for it.

## 9. Performance and stability

Owner metrics during the drill:

| Metric | Value |
| --- | --- |
| Embedding latency p50 | 158 ms |
| Embedding latency p95 | 180 ms |
| Search latency p50 | 164 ms |
| Extraction latency, single sample | 3.7 s |
| Upsert throughput p50 | 7.4 points/s |

The bounded soak ran for 20 minutes. It issued 42 sequential write and read
pairs through one device credential and restarted the Owner 10 times.

| Metric | Value |
| --- | --- |
| Write and read pairs | 42 |
| Searchable after write | 38 |
| Read latency p50 | 1093 ms |
| Read latency p95 | 1162 ms |
| Restart readiness, warm | 1.0 to 1.1 s |
| Restart readiness, first start | 91 s |

Four attempts fell inside the first restart window. Each returned an Owner error
and produced no record. The harness counted those attempts as writes, and the
report keeps the raw count. The first start pays embedded storage and model
initialisation. Later restarts return in about one second.

The latency includes process start for the command-line client. It describes a
synthetic load on two local cores, not a production workload.

## 10. Reviews

| Source | Scope | Result |
| --- | --- | --- |
| Kiro CLI | diff against `9c1e0d6` | No P0. Crash consistency, exactly-once, gates and authorization hold. One P2 on auth rotation. |
| OpenCode | same diff | No P0. One P1 on provider status classification. Four P2 on invariants, backlog and tests. |
| CodeRabbit | not available | NOT VERIFIED |

Disposition:

- P1 provider status: fixed. The Owner now raises a typed status error, and the
  classifier maps 401, 403, 429 and other statuses. A test covers each code.
- P2 quarantine invariant: fixed. The ledger refuses a prepared row.
- P2 backlog cap: fixed. Retained events count against the cap.
- P2 missing tests: fixed. Re-submission, post-extraction failure and status
  mapping now have cases.
- P2 auth rotation: accepted. The event is retained, and the operator fixes the
  key and retries. The ADR records the trade-off.

## 11. Commits and pull requests

| Repository | Content | Pull request |
| --- | --- | --- |
| Runtime | quarantine, diagnostics, tests, ADR and docs | [#95](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/95) |
| MCP | Pi adapter acknowledgement | [#45](https://github.com/cnkang/qdrant-memory-mcp/pull/45) |

Neither pull request is merged and no release is published from this work.

## 12. Release and rollback risk

The change adds one status value and one counter. Collections, receipts and
credentials keep their meaning. An older Runtime skips an unknown status in
replay, so a rollback keeps the data and stops automatic retry.

The Runtime release must precede the adapter release. The released 0.5.1 does not
return `QUARANTINED`, so the adapter change is safe with it as well.

## 13. Open items

1. Repeat D2 concurrency on the branch.
2. Run a 24-hour soak before an unattended deployment.
3. Decide whether rotating credentials should treat 401 as transient.
4. Reduce the double retention of a quarantined turn between the event row and
   the admission receipt.

## 14. Recommendation

The branch meets the P0 goal on real devices. One Pi extraction failure no longer
stops the other agents, the retained event commits exactly once after a retry,
and the fail-closed paths still hold.

Do not present this as a long-term unattended deployment yet. Complete the 24-hour
soak and the concurrency group first.
