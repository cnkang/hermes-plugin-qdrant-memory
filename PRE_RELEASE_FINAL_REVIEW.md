# Hermes Qdrant Memory v0.1.0 — Final Pre-release Review

Review date: 2026-10-08 (Asia/Singapore)
Base: remote `main` at `a6a883cd3db3bfdf57cb923f1e9c26fb336df211`
Branch: `codex/v0.1.0-pre-release-hardening`
PR: to be linked after opening; this work is not merged or released.

## Release decision

**NOT READY**

The confirmed replay-after-delete defect is fixed and locally reproduced. This report
does not recommend a release yet: current-head GitHub checks and the latest-Hermes
run are outstanding, and the plugin still has material durability and retention
limits described below. No tag or release was created.

## Baseline and historical evidence

- The branch starts from the latest `origin/main` fetched for this task. That base is
  merge commit `a6a883cd3db3bfdf57cb923f1e9c26fb336df211` (PR #10).
- On that base SHA, the historical CI, platform compatibility and optional Cloud
  smoke runs were green: [CI](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37761771970),
  [platform compatibility](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37761771784),
  and [Cloud smoke](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37761771815).
  These results do not validate this branch.
- The upstream Hermes `main` SHA sampled before dispatch was
  `328a75e2b140000069c5775e1fdf75a1a734dddc`. The branch's latest-Hermes workflow
  records the SHA actually checked out; that run is not yet recorded here.
- Tests use temporary local profiles, in-memory or temporary embedded Qdrant, and
  synthetic records. No user memory collection was migrated or reset.

## Confirmed finding

### Prepared UPSERT could restore a deleted fact

An event could be admitted and prepared into an UPSERT, fail before Qdrant accepted
it, then remain pending for retry. If another source wrote the same fact under a
different point ID and that fact was deleted, the old retry could bypass the
prepare-time fence because commit did not recheck the delete. The retry could
therefore restore data the user had deleted.

The new regression reproduces both the same-point and cross-point/same-hash cases.
Before the fix, the cross-point case failed with the stale operation committed after
delete. `_commit_action` now rechecks the scope-bound fence immediately before each
UPSERT batch, comparing point ID and content hash, and marks fenced operations
`SUPERSEDED`. A later, newly admitted write is still allowed to add the fact again.
The existing failed-DELETE retry, scope-isolation and timestamp-ordering contracts
remain in the test suite.

## Implementation

- Added a commit-time Delete Fence check for replayable UPSERTs.
- Added SQLite indexes for event/operation replay, open-point checks, and delete
  fences by point ID or content hash. Existing ledger schemas gain these indexes via
  `CREATE INDEX IF NOT EXISTS`; existing event and operation rows are preserved.
- Added tests for the prepared stale UPSERT, same/cross point IDs, lookup query plans,
  and opening an existing ledger without losing replay rows.
- Extended latest-Hermes compatibility to install the plugin distribution, load its
  `hermes_agent.memory_providers` entry point, and run the manifest parser contract
  on Python 3.11 and 3.14. The workflow records exact plugin and Hermes SHAs.
- Updated English and Chinese README pre-release notices and the changelog. Detailed
  historical validation remains in [docs/validation.md](docs/validation.md).

## Validation

| Check | Result | Evidence |
|---|---|---|
| Full local suite on Python 3.11.15 and 3.14.0 with Hermes checkout `4787e4d56fc8d9265d4c7d3c0fe5accee86b4078` | **PASS** | 216 passed and 3 remote-service tests skipped on each version; one expected local-Qdrant payload-index warning. 3.11: 8.04 s; 3.14: 5.03 s. |
| Focused Delete Fence and retained-history tests | **PASS** | 10 passed. |
| Ruff 0.15.1 lint and format | **PASS** | `ruff check .`; `ruff format --check .`. |
| GitHub Actions workflow syntax | **PASS** | `actionlint .github/workflows/*.yml`. |
| Installed distribution smoke | **PASS** | Installed from the checkout into the isolated test environment; version 0.1.0, entry point loaded as `qdrant_memory:register`, packaged manifest present. |
| Retrieval pilot | **PASS** | Real Ollama `qwen3-embedding:4b`, 2,560 dimensions; 21 synthetic memories and 24 queries; Recall@1 0.806, Recall@5 0.986, Recall@10 1.000, MRR 0.931, search p95 125.25 ms. This fixture is not a production recall guarantee. |
| 100,000-row lookup probe | **PASS** | 100 absent-point lookups took 0.0003 s with the compound index and 0.5791 s after dropping it; query plans use the intended point and hash indexes. Synthetic temporary database only. |
| Local live Qdrant Server/Cloud integration | **SKIPPED** | Two Server cases required `--qdrant-test-url`; the Cloud case requires secret-gated CI configuration. |
| Latest-Hermes workflow on current `main` | **NOT RUN** | Workflow is configured to test Python 3.11/3.14 and record the exact Hermes SHA. |
| Current-branch GitHub CI and platform matrix | **NOT RUN** | Must be checked on the PR's current head SHA. |
| Current-branch authenticated Cloud smoke | **NOT RUN** | The workflow is manually dispatched and uses a disposable collection; no run is recorded yet. |

The 100,000-row performance probe measured lookup time only. A separate synthetic
retention probe found a 100,000-operation pending backlog can materialize about
132 MiB in Python during recovery when rows contain 329-byte bodies. Recovery still
loads the backlog into memory in one pass; paging is not implemented.

## Reset recovery review

Existing tests use hard process exits after remote deletion/before collection
recreation and after recreation/before identity validation, then verify `init`
resumes the authorized reset while retaining other destinations. Injected-failure
tests cover additional reset errors. A hard process exit specifically after durable
intent commit/before deletion, after ledger clear/before intent removal, and during a
second recovery attempt was **NOT RUN**. No deterministic reset implementation
defect was confirmed in the reviewed paths.

## Remaining limitations and risks

- Hermes currently queues provider callbacks in memory. Durability starts only after
  the plugin callback writes the event to SQLite. A host crash or bounded shutdown
  can lose a callback that has not reached that admission point.
- Ledger rows and migration manifests do not expire by time. Pending/failed work
  keeps its body, and an incomplete migration can keep terminal operation bodies
  pinned. Logical scrubbing or reset does not guarantee secure erasure from SQLite
  pages, WAL/SHM files, snapshots, backups, or copied storage.
- The 100,000-operation open-backlog probe shows recovery can use substantial memory.
- Reset crash testing does not cover every durable transition with a process kill.
- Retrieval metrics come from a small labeled synthetic dataset. They do not predict
  production recall or user satisfaction.
- Live Server and Cloud checks for this branch, current-head GitHub CI, and current
  latest-Hermes compatibility are not yet evidenced.

## Final status

**NOT READY** until the outstanding current-head and latest-Hermes checks are
terminal and the remaining durability/retention risks have an accepted release
boundary. The PR is for review and discussion; this task does not merge it, tag a
version, or publish a release.
