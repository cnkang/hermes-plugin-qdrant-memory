# v0.1.0 Pre-release Checklist

## Post-merge status (2026-10-09)

PRs #10–#14 are merged. Current main
`f6a2e97f9cd16c4aed3ba2590bfcf36176ee5e29` has the following recorded results:

- **PASS** — [CI](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37868895051), including the required security gate.
- **PASS** — [Six platform jobs](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37868895110).
- **PASS** — [Authenticated Cloud integration](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37868895002); the test step executed, without changing main-only protection.
- **PASS** — Live Quick Start drill (2026-10-09): a fresh home with local Ollama completed install, automatic extraction, cross-session recall, update, delete and restart; see [validation evidence](../validation.md).
- **READY FOR LIMITED TECHNICAL PREVIEW** — One writer per destination across all profiles/hosts; documented admission, retention and capacity limits remain.
- **NOT PUBLISHED** — No tag or GitHub Release. These results do not declare public beta.

Earlier snapshot (2026-10-08): PRs #10–#12 merged; main
`32bd4e7abdb3b417cc5b0dd0f793f93db188db3b` passed
[CI](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37802338914),
[six platform jobs](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37802339623)
and [authenticated Cloud integration](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37802338838).

Evidence below records pre-merge candidates and does not override this snapshot.

## Historical PR #12 candidate (before merge)

Base main: `79565e755716f3d812b73e550417b5049b9642e6`.
Branch: `fix/v0.1.0-final-release-hardening`.
PRs #10 and #11 are merged. Candidate results and the release decision are in
[PRE_RELEASE_FINAL_REVIEW.md](PRE_RELEASE_FINAL_REVIEW.md).
Pending gates are not PASS.

- **PASS** — Migration cancellation is terminal without claiming an uncommitted write;
  old and completed manifests resume without authorizing restoration after deletion.
- **PASS** — Operations/events scan 0–100,000 synthetic rows in bounded rowid pages;
  stable watermarks, failure continuation, retry, locks and hard-exit recovery tested.
- **PASS** — All missing Reset hard process-exit transitions and interrupted recovery.
- **PASS** — Latest-Hermes two-version canonical and independent wheel validation;
  exact implementation evidence is in the final report.
- **PASS** — Disposable Server REST/gRPC and restart, including fenced migration.
- **BLOCKED BEFORE TEST** — Candidate Cloud run 37791397024 rejected by main-only
  environment protection. No credentials or protection rules were changed.
- **NOT RUN** — Full interactive Quick Start against a live Ollama/LLM; deterministic
  fresh-profile discovery/setup/CLI and packaged-wheel smoke are tested separately.
- **READY FOR LIMITED TECHNICAL PREVIEW** — Single writer and documented admission /
  retention limits; public beta requires protected Cloud and exact-head remote gates.
- **NOT RUN** — Merge, tag and GitHub Release, intentionally outside this task.

## Historical PR #11 checklist (before merge)

The items below describe the earlier PR #11 implementation and its then-current
Cloud rejection; later main Cloud tests passed (see the final report).

Historical review: 2026-10-08. The following checklist records evidence only for
the then-current [PR #11](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/11)
branch, before its merge; it does not validate PR #12.
See [PRE_RELEASE_FINAL_REVIEW.md](PRE_RELEASE_FINAL_REVIEW.md) for details.

## Code and data safety

- **PASS** — Branch created from the latest fetched `origin/main` SHA
  `a6a883cd3db3bfdf57cb923f1e9c26fb336df211`.
- **PASS** — Existing untracked `IDEA.md` was left untouched and is excluded from
  the PR.
- **PASS** — Reproduced the stale prepared-UPSERT replay, then fixed and tested both
  same-point and cross-point/same-hash deletion cases.
- **PASS** — Scope isolation, failed DELETE retry and intentional post-delete re-add
  remain covered by regression tests.
- **PASS** — Ledger lookup indexes are added in place; a test verifies existing
  events/operations survive opening the old schema.
- **PASS** — Reviewed current scope enforcement, transcript provenance quarantine,
  profile database permissions, and sensitive backup/WAL documentation; no new
  scope bypass was found in this change.
- **PASS** — Tests and retrieval measurements use temporary synthetic data; no user
  collection was modified.

## Local validation

- **PASS** — Full test suite on Python 3.11.15 and 3.14.0: 216 passed and 3
  remote-service tests skipped on each version, with one expected local-Qdrant
  payload-index warning per run.
- **PASS** — Ruff 0.15.1 lint and formatting checks.
- **PASS** — `actionlint` over all GitHub Actions workflows.
- **PASS** — Isolated installed-distribution smoke: provider entry point loads and
  packaged `plugin.yaml` is present.
- **PASS** — Synthetic 24-query retrieval pilot completed using local Ollama and a
  temporary embedded Qdrant collection.
- **PASS** — 100,000 synthetic pending operations: indexed lookup and point/hash
  query plans verified.
- **SKIPPED** — Two local Server tests require an explicit disposable endpoint.
- **PASS** — PR CI Qdrant Server REST/gRPC integration lane on implementation
  commit `a81d09c` ([run 37766236440](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37766236440)).

## Reset, host and live-service gates

- **PASS** — Existing reset tests cover process death after Qdrant deletion/before
  collection recreation and after recreation/before identity validation, plus
  recovery preserving other destinations.
- **NOT RUN** — Hard process death immediately after reset-intent commit and before
  delete; after ledger clear and before intent removal; and during a second recovery
  attempt. Additional reset errors have injected-failure coverage.
- **PASS** — Latest-Hermes workflow checked out `e6848c2c9a86e84d9a5c672085bb7d79079a66ba`
  and passed on Python 3.11 and 3.14 ([run 37766274748](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37766274748)).
- **PASS** — PR CI on implementation commit `a81d09c` ([run 37766236440](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37766236440)).
- **PASS** — Linux/macOS/Windows × Python 3.11/3.14 platform matrix on implementation
  commit `a81d09c` ([run 37766236512](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37766236512)).
- **BLOCKED BEFORE TEST** — Authenticated Cloud smoke ([run 37766278783](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/37766278783)) was rejected by `qdrant-cloud` environment protection before any test step. Cloud behavior remains unvalidated.
- **MANUAL REVIEW REQUIRED** — CodeRabbit's green status says its review was skipped
  for this OSS repository; it does not represent a completed CodeRabbit review.
- **PASS** — Sourcery review on documentation follow-up commit `d8a1f3d` completed
  and found no blocking security issues. Sourcery was skipped on implementation SHA
  `a81d09c`.

## Release gate

- **NOT READY** — CI, platform compatibility and latest-Hermes checks passed on the
  implementation commit, but Cloud behavior was not tested and the durability/
  retention risks documented in the final review have no accepted release boundary.
- **PASS** — Changes are prepared for PR review only; no merge or release action was
  taken.
