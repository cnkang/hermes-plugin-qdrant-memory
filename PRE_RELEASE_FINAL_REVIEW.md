# Hermes Qdrant Memory v0.1.0 — Final Pre-release Review

Review date: 2026-10-08 (Asia/Singapore)
Base: remote `main` at `a6a883cd3db3bfdf57cb923f1e9c26fb336df211`
Branch: `codex/v0.1.0-pre-release-hardening`
PR: [#11 — fix: prevent stale memory replay after delete](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/11); this work is not merged or released.

## Release decision

**NOT READY**

The confirmed replay-after-delete defect is fixed and locally reproduced. CI,
platform compatibility, and latest-Hermes checks passed on implementation commit
`a81d09c`. The Qdrant Cloud smoke was blocked by environment protection before its
test ran. The plugin also has material durability and retention limits described
below. No tag or release was created.

## Baseline and historical evidence

- The branch starts from the latest `origin/main` fetched for this task. That base is
  merge commit `a6a883cd3db3bfdf57cb923f1e9c26fb336df211` (PR #10).
- On that base SHA, the historical CI, platform compatibility and optional Cloud
  smoke runs were green: [CI](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37761771970),
  [platform compatibility](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37761771784),
  and [Cloud smoke](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37761771815).
  These results do not validate this branch.
- Latest-Hermes workflow run [37766274748](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37766274748)
  checked out upstream Hermes `main` at
  `e6848c2c9a86e84d9a5c672085bb7d79079a66ba`; both Python 3.11 and 3.14 jobs passed.
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
| PR CI on implementation commit `a81d09c` | **PASS** | [Run 37766236440](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37766236440): behavior contracts, Ruff, Snyk dependency/code scans, SonarCloud, and the Qdrant Server REST/gRPC integration lane passed. |
| Platform compatibility on implementation commit `a81d09c` | **PASS** | [Run 37766236512](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37766236512): Linux, macOS and Windows × Python 3.11 and 3.14. |
| Latest-Hermes compatibility | **PASS** | [Run 37766274748](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37766274748): Python 3.11 and 3.14; wheel install through Hermes PM, installed entry point and manifest, and host lifecycle contracts passed against Hermes `e6848c2c9a86e84d9a5c672085bb7d79079a66ba`. |
| Local optional live Qdrant tests | **SKIPPED** | Two local Server cases require an explicit `--qdrant-test-url`. The PR CI Server REST/gRPC integration lane passed. |
| Authenticated Qdrant Cloud smoke | **BLOCKED BEFORE TEST** | [Run 37766278783](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37766278783) failed before any test job step: the branch is not allowed to deploy to the protected `qdrant-cloud` environment. Cloud behavior remains unvalidated. |
| Automated PR review integrations | **SKIPPED** | CodeRabbit reported “Review skipped: manual review required” for this OSS repository; Sourcery review was skipped. The green CodeRabbit status is not a completed bot review. |

Remote validation above is linked to implementation commit `a81d09c`. A later
documentation-only follow-up records these results; the PR page shows checks for
its current head separately.

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
- Qdrant Cloud was not validated: branch protection stopped the workflow before
  tests. The PR CI Server REST/gRPC lane did pass.

## Final status

**NOT READY.** CI, platform compatibility, and latest-Hermes checks passed for the
implementation commit, but Cloud behavior remains unvalidated and the remaining
durability/retention risks need an accepted release boundary. The PR is for review
and discussion; this task does not merge it, tag a version, or publish a release.
