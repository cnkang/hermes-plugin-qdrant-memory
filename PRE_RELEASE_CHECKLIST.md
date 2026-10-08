# v0.1.0 Pre-release Checklist

Last reviewed: 2026-10-08. This checklist records evidence for the current hardening
branch and [PR #11](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/11).
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
- **SKIPPED** — CodeRabbit indicated manual review is required for this OSS repository;
  Sourcery review was skipped.

## Release gate

- **NOT READY** — CI, platform compatibility and latest-Hermes checks passed on the
  implementation commit, but Cloud behavior was not tested and the durability/
  retention risks documented in the final review have no accepted release boundary.
- **PASS** — Changes are prepared for PR review only; no merge or release action was
  taken.
