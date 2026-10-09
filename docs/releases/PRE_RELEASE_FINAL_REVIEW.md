# Hermes Qdrant Memory v0.1.0 — Final Release Acceptance

## Earlier post-merge snapshot (2026-10-08)

Superseded by the 2026-10-09 snapshot in [validation](../validation.md); retained
as the dated record for PRs #10–#12.

PRs #10, #11 and [#12](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/12)
are merged. That post-merge main revision was
`32bd4e7abdb3b417cc5b0dd0f793f93db188db3b`.
[CI](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37802338914),
[all six platform jobs](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37802339623)
and [authenticated Cloud integration](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37802338838)
passed on that exact SHA. The Cloud exercise step actually ran; the candidate branch
rejection below no longer represents the merged code's validation status.

Status remains **READY FOR LIMITED TECHNICAL PREVIEW** with one writer per destination
across all profiles and hosts. No tag or GitHub Release has been published. A live
interactive Quick Start drill was completed on 2026-10-09 on a fresh home with local
Ollama (see [validation](../validation.md)), and admission, retention and synthetic
retrieval/capacity limits still apply. Passing these gates does not declare public
beta or authorize a release. See [validation](../validation.md) for the current
snapshot; the following candidate reports and decisions are historical records.

## Historical PR #12 candidate (2026-10-08, before merge)

Base: `79565e755716f3d812b73e550417b5049b9642e6` (latest main fetched for this task).
Branch: `fix/v0.1.0-final-release-hardening`.
PR: [#12](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/12) (open; not merged).
Implementation SHA: `c77718edce269898db6b47088489c204cd38b234`.
Subsequent follow-ups strengthen two replay assertions and add a typed CLI conflict
error; their final-head gates are linked from [PR #12](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/12).
The PR review follow-up below records additional changes after `b957e43`; the
implementation runs in the tables remain evidence for their stated SHA only.
Decision: **READY FOR LIMITED TECHNICAL PREVIEW** for one-writer deployments within
the documented admission, storage and retrieval limits. Embedded and Server paths
are locally validated. **Public beta requires candidate Cloud validation and all
exact-head remote gates**. Cloud is blocked by the existing main-only environment
policy, not by a demonstrated candidate data defect. No merge, tag or Release is created.
Implementation remote validation has completed. Historical PASS results in the
archived PR #11 section below do not certify this candidate.

PR #10 and PR #11 are merged. On this main SHA,
[CI](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37788043978),
[six platform jobs](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37788044014),
and [authenticated Cloud integration](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37788044033)
passed. Cloud tests actually executed. The earlier branch environment rejection is historical.

| Finding | Severity | Status | Fix | Tests | Residual risk |
|---|---|---|---|---|---|
| Migration SUPERSEDED | P0/P1 | FIXED; local PASS | Terminal accounting and explicit conflict semantics | 8 lifecycle regressions plus existing migration tests | Resume preserves later deletion intent; conflicts need explicit review |
| Recovery memory growth | P1 | FIXED; local PASS | Bounded keyset scan and finite scan watermark | 0–100,000 operations/events, ordering and hard-exit recovery | Retained history and individual manifests still grow |
| Reset crash windows | P1 | PASS locally | No core change needed | 18 subprocess/reset tests | Remote reset is recoverable, not atomic |
| Stale release documentation | P1 | FIXED | Separate merged-main evidence from candidate results | Status audit and linked run evidence | Final release remains a human action |
| Latest Hermes compatibility | P1 | PASS locally | Independent latest-main lane retained | Python 3.11 and 3.14 wheel contracts against `25a71a744cb9ef06950a91638e6229b4f808d461` | Upstream can change after validation |
| Qdrant Cloud validation | P1 | BLOCKED BEFORE TEST | Keep main-only protection intact | Candidate [run 37791397024](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37791397024) rejected; job has no steps | Candidate requires protected release gate |

### Remote implementation gates

All runs below checked out implementation `c77718edce269898db6b47088489c204cd38b234`.

| Gate | Result | Evidence |
|---|---|---|
| CI: six immutable Hermes/Python lanes, Server REST/gRPC, wheel, Ruff, SonarCloud, Snyk dependency/code, required gate | PASS | [37791377125](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37791377125), all 12 jobs successful |
| Linux/macOS/Windows × Python 3.11/3.14 | PASS | [37791376772](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37791376772), all six jobs successful |
| Latest-Hermes wheel + callback/manifest/Host lifecycle, Python 3.11/3.14 | PASS | [37791391129](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37791391129), actual Hermes checkout `38880bd2f1e90dbc9a1aeec03af62539ee64719a` on both jobs |
| Authenticated Cloud | BLOCKED BEFORE TEST | [37791397024](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37791397024); branch disallowed by protected environment, zero test steps |
| Sourcery review | Completed; findings triaged | [PR review findings](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/12#discussion_r4220124125); check is FAILURE, not a green security gate |
| CodeRabbit | Completed after manual request | Reviewed base through `5c6b4dd`; two documentation comments corrected in the final follow-up. Initial automatic skip was not counted as review completion |

### PR #12 review follow-up

| Review finding | Disposition | Regression evidence |
|---|---|---|
| Progress reads each full payload twice | FIXED: count pending keys in static, parameterized batches of 128, reading only keys | Exact progress totals, duplicate/missing/failed/foreign keys, hostile-shaped keys, unsized generators; 1,000 keys need eight count queries |
| SQL construction in terminal cleanup | HARDENED: two complete static statements retain the partial indexes, rowid bounds and page size | Existing terminal scrubbing/recovery tests |
| SQL construction in benchmark EXPLAIN | HARDENED: complete static EXPLAIN statements bind every value | Both real SQLite plans use the collection/status index; bounded plan keeps rowid bounds without a temporary sort |
| Two independent Runtime writers race with DELETE | Outside supported ownership: manually constructed Runtime instances bypass WriterLease | Four production ownership tests block a second provider, mutating CLI and child-process lease during an in-flight write; one provider serializes DELETE after UPSERT |

The two SQL warnings did not establish injection: their original fragments were
internal constants. Static statements remove the construction altogether without
suppressing the checker. Progress still visits O(n) lightweight keys for an exact
initial total, but no longer loads every payload or issues one count query per key.
No manifest, ledger schema or deletion semantics change in this follow-up.
The count query explicitly uses the existing primary-key index: the default planner
otherwise chose a collection/status scan for every batch during the 100,000-record
probe. A query-plan regression prevents that quadratic scan pattern. With 100,000
synthetic pending operations and 1,024-byte text fixtures on local Python 3.14.7,
the old full-row pre-count took 1.1371 s / 100,000 queries; the fixed key-only count
took 0.1274 s / 782 queries. Both counted 100,000. `tracemalloc` measured 29,515 vs
33,697 peak bytes, excluding the already allocated input key list; both counts
remain bounded in auxiliary memory. This is a count-only probe using the benchmark
seed helper, SQLite trace callback and `time.perf_counter`, not Qdrant throughput.

The concurrent-Runtime scenario is reproducible when callers bypass production
ownership. Provider and mutating CLI entries acquire WriterLease before services
or Runtime are constructed, and one Runtime serializes mutation with its lock.
Separate profiles/hosts writing the same remote destination remain unsupported;
the lease is not distributed. The fence-cursor warning does not reproduce: cursor
iteration and closure are inside `with self.lock`. Two coverage gaps and both
CodeRabbit documentation comments were fixed in the earlier follow-ups; they
remain covered rather than being implemented again.

Follow-up validation uses disposable fixtures only. Local Python 3.11/3.14 full
suite results and exact-head remote outcomes are recorded in the
[PR #12 validation updates](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/12).
The benchmark's 0/1/100-record smoke run passed all correctness assertions after
the static EXPLAIN change; the large-scale measurements below retain their original
execution provenance. Candidate Cloud remains blocked by the main-only environment.

### Root causes and state contract

The old counter omitted SUPERSEDED, leaving a fully canceled migration permanently
incomplete. Drift repair on `--resume` could also replan an already completed ADD
or SKIP after a scoped user deletion, unintentionally granting a new write admission.
Both paths are fixed. Old snapshots remain settled with explicit canceled records;
`--verify` fails on superseded records and reports actual missing/mismatched data.
Only a fresh import or a changed source snapshot authorizes a new plan.
The actual CLI JSON boundary returns nonzero with `code: migration_superseded` and
a fixed, sanitized inspection/reimport hint. A regression verifies that generic
error scrubbing cannot hide this actionable conflict or leak source text.

`processed` counts each terminal source record once. `applied`, `added`, and
`updated` count actual acknowledged COMMITTED operations, including writes later
deleted; `superseded` can overlap those historical counts. A fenced operation that
never committed contributes zero applied writes. SKIP is never an applied write.
Old schema-v1 manifests remain readable; matching source data supplies optional
content hashes on resume. Missing operations remain failures rather than canceled
successes. Completed manifests retain audit/resume metadata and permit terminal
payload scrubbing.

Recovery now uses rowid keyset pages of 128 under fixed operations/events upper
watermarks. Commit reads only configured-size payload batches and rechecks status.
No replay cursor or Ledger lock crosses embedding/Qdrant/LLM service calls.
New admissions wait for a later round; status changes cannot shift pages. Cleanup
uses bounded pages plus narrow partial indexes excluding already scrubbed history.
Delete fence lookup streams its rows without materializing matching tombstones.
No automatic retention deletion was introduced.

### Candidate local evidence

Tests use disposable profiles and synthetic memories only. Latest Hermes main:
`25a71a744cb9ef06950a91638e6229b4f808d461`.
Canonical runner on implementation `c77718edce269898db6b47088489c204cd38b234`:
Python 3.11.15 **257 PASS, 0 FAIL, 1 SKIPPED** (30.4 s) and Python 3.14.7
**257 PASS, 0 FAIL, 1 SKIPPED** (33.4 s). Each run actually includes disposable
Qdrant 1.15.5 Server REST/gRPC, container restart and the new fenced-migration
lifecycle exercise. The only skip is Cloud without credentials.
Wheel smoke using independent site-packages installations on Python 3.11.15 and
3.14.0: **PASS** (entry point, packaged manifest, schema persistence, real CLI discovery).
Each isolated wheel environment additionally passes nine real installed-wheel
callback/Host/shutdown/recovery contracts, without the source checkout import override.
Wheel SHA256: `a6cd71254d4d6b208a54ef075d525ae95fa77718bfd214449ec4cc32fec00745`.
Latest host lifecycle admission/shutdown contracts with `HERMES_QDRANT_REQUIRE_HOST_SYNC=1`:
**4 PASS**. Ruff check/format, actionlint and diff check: **PASS**.
These local results do not establish Cloud or cross-platform compatibility.

Reproduce the canonical run from the exact Hermes checkout, with the test environment
selected for Python 3.11 or 3.14 and a disposable Qdrant container:

```sh
HERMES_PYTHON=/path/to/isolated-env/bin/python scripts/run_tests.sh \
  /path/to/hermes-plugin-qdrant-memory/tests -j 1 --file-retries 0 -- \
  --tb=short --qdrant-test-url=http://127.0.0.1:6333 \
  --qdrant-test-container=hermes-hardening-disposable-qdrant
```

Full interactive Quick Start using a real Ollama/LLM: **NOT RUN** this round.
Fresh-profile directory discovery, setup schema and CLI are covered with deterministic
services; this is not a claim that a live model was provisioned. The Server/client
version-difference warning (1.15.5/1.19.1) remains visible; tested behavior passed.

### Performance and privacy measurements

[Reproducible benchmark](../recovery-benchmark.md) and
[script](../../scripts/benchmark_recovery.py) validate 0/1/100/1,000/10,000/100,000
synthetic pending operations on implementation `c77718e`. At 100,000, actual recovery
Python `tracemalloc` peak is **0.4964 MiB**, compared with **163.6766 MiB** using
the former list reader in the same current runtime (99.697% reduction).
Recovery durations: **29.859949 s** bounded versus **30.675616 s** former reader.
These measurements include FULL SQLite acknowledgement transactions and fake
constant-memory store calls; they are not network throughput or whole-process RSS.
EXPLAIN confirms indexed collection/status/rowid range queries without OFFSET.
The scrubbed database remained **138,539,008 bytes**, WAL **139,383,752 bytes**
before close; final close/reopen removed that WAL. No physical shrink or secure
erasure is promised. Terminal reopen used 8,004 Python bytes at peak.

Latest Hermes `agent/memory_manager.py` still uses an in-memory executor/Future
queue (`_submit_background`, `sync_all`, bounded `shutdown`). The regression
`test_host_shutdown_reports_turns_not_yet_admitted_to_provider` passes. Durability
starts at plugin SQLite admission. WriterLease remains local process coordination;
deploy one writer per destination and stop writers on every host for maintenance.

Pending/FAILED payloads remain necessary recovery inputs. Completed manifests and
terminal identity/hash rows remain necessary resume/idempotency/deletion evidence.
Logical scrub is not secure erase of old pages, WAL/SHM, snapshots or backups.
Retention compaction is deferred; see [capacity procedures](../operations.md)
and the synthetic recovery benchmark. Dense retrieval quality is workload dependent.
Repository Topics and Homepage remain unset; publishing metadata, a tag and a
GitHub Release are manual follow-up actions. No release action is authorized here.

## Historical PR #11 review (before merge)

The remainder records the earlier review at base `a6a883c`, including its then-current
NOT READY decision. It is retained as historical evidence and does not describe the
current PR or the current Cloud validation state.

Review date: 2026-10-08 (Asia/Singapore)
Base: remote `main` at `a6a883cd3db3bfdf57cb923f1e9c26fb336df211`
Branch: `codex/v0.1.0-pre-release-hardening`
PR: [#11 — fix: prevent stale memory replay after delete](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/11); this work is not merged or released.

## Historical PR #11 release decision

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
  historical validation remains in [docs/validation.md](../validation.md).

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

## Historical PR #11 final status

**NOT READY.** CI, platform compatibility, and latest-Hermes checks passed for the
implementation commit, but Cloud behavior remains unvalidated and the remaining
durability/retention risks need an accepted release boundary. The PR is for review
and discussion; this task does not merge it, tag a version, or publish a release.
